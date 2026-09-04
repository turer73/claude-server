"""durable_dispatcher testleri — cursor kaliciligi, payload cozumu ve KAYIP-EVENT davranisi.

Bu modul de 2026-08-27'de commit edilmeden production'a girdi ve 8 gun testsiz kostu
(PR#380 ile repoya alindi). Kesif #1676 deneyi geregi main.py'de KAPALI
(`presence_dispatcher_enabled = False`); testler modulu dogrudan kurup mantigi dogrular,
boylece kapi 09-10'da geri acildiginda davranis kanitli olur.

Hicbir test CANLI server.db'ye dokunmaz: `server_db_path` tmp_path'e monkeypatch edilir.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.core import durable_dispatcher as dd


class FakeBus:
    """publish'i kaydeden sahte bus; fail_ids'teki event id'lerinde hata firlatir."""

    def __init__(self, fail_ids: set[int] | None = None) -> None:
        self.published: list = []
        self.fail_ids = fail_ids or set()
        self.publish_calls = 0

    async def publish(self, event) -> None:
        self.publish_calls += 1
        if event.id in self.fail_ids:
            raise RuntimeError(f"bus down (id={event.id})")
        self.published.append(event)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Izole server.db + events tablosu + schema-cache sifirlanmis."""
    path = str(tmp_path / "server.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, source TEXT, payload TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(dd, "server_db_path", lambda: path)
    monkeypatch.setattr(dd, "_schema_ready", False)
    return path


def _insert(path: str, type_: str, source, payload) -> int:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("INSERT INTO events (type, source, payload) VALUES (?, ?, ?)", (type_, source, payload))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _cursor_rows(path: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='event_dispatch_cursor'").fetchone():
            return []
        return list(conn.execute("SELECT id, cursor FROM event_dispatch_cursor"))
    finally:
        conn.close()


# --- cursor kaliciligi --------------------------------------------------------


def test_load_cursor_on_fresh_db_is_zero_and_creates_table(db):
    """Ilk acilis: cursor 0, tablo _conn() tarafindan yaratilmis olmali."""
    d = dd.DurableEventDispatcher(FakeBus())
    assert d._load_cursor() == 0
    assert _cursor_rows(db) == []  # tablo var ama satir yok


def test_save_then_load_cursor_roundtrip(db):
    d = dd.DurableEventDispatcher(FakeBus())
    d._save_cursor(42)
    assert _cursor_rows(db) == [(1, 42)]
    assert dd.DurableEventDispatcher(FakeBus())._load_cursor() == 42


def test_save_cursor_upserts_single_row(db):
    """id=1 tek satir kisiti: ikinci kayit yeni satir degil UPDATE olmali."""
    d = dd.DurableEventDispatcher(FakeBus())
    d._save_cursor(7)
    d._save_cursor(9)
    assert _cursor_rows(db) == [(1, 9)]


def test_load_cursor_survives_db_error(tmp_path, monkeypatch):
    """DB acilamazsa 0 don, patlama — restart yolunu kilitlememeli."""
    monkeypatch.setattr(dd, "server_db_path", lambda: str(tmp_path / "yok" / "server.db"))
    monkeypatch.setattr(dd, "_schema_ready", False)
    assert dd.DurableEventDispatcher(FakeBus())._load_cursor() == 0


def test_save_cursor_survives_db_error(tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "server_db_path", lambda: str(tmp_path / "yok" / "server.db"))
    monkeypatch.setattr(dd, "_schema_ready", False)
    dd.DurableEventDispatcher(FakeBus())._save_cursor(5)  # sessiz gecmeli


# --- _fetch_rows --------------------------------------------------------------


def test_fetch_rows_only_after_cursor_and_ordered(db):
    for i in range(5):
        _insert(db, f"t{i}", "src", "{}")
    d = dd.DurableEventDispatcher(FakeBus())
    d.cursor = 2
    rows = d._fetch_rows()
    assert [r["id"] for r in rows] == [3, 4, 5]


def test_fetch_rows_respects_batch_limit(db, monkeypatch):
    monkeypatch.setattr(dd, "_BATCH", 3)
    for i in range(10):
        _insert(db, f"t{i}", "src", "{}")
    rows = dd.DurableEventDispatcher(FakeBus())._fetch_rows()
    assert [r["id"] for r in rows] == [1, 2, 3]


# --- _poll: publish + payload -------------------------------------------------


async def test_poll_publishes_with_from_db_and_db_id(db):
    """Loop-guard'in tasiyicisi: from_db=True ve event.id = DB satir id'si olmali."""
    rid = _insert(db, "thought:new", "critic", json.dumps({"k": "v"}))
    bus = FakeBus()
    d = dd.DurableEventDispatcher(bus)
    await d._poll()

    (ev,) = bus.published
    assert ev.type == "thought:new"
    assert ev.source == "critic"
    assert ev.payload == {"k": "v"}
    assert ev.from_db is True
    assert ev.id == rid


async def test_poll_null_source_becomes_empty_string(db):
    _insert(db, "t", None, "{}")
    bus = FakeBus()
    await dd.DurableEventDispatcher(bus)._poll()
    assert bus.published[0].source == ""


async def test_poll_invalid_json_payload_kept_as_raw(db):
    _insert(db, "t", "src", "bu json degil")
    bus = FakeBus()
    await dd.DurableEventDispatcher(bus)._poll()
    assert bus.published[0].payload == {"_raw": "bu json degil"}


async def test_poll_invalid_json_raw_truncated_to_200(db):
    _insert(db, "t", "src", "x" * 500)
    bus = FakeBus()
    await dd.DurableEventDispatcher(bus)._poll()
    assert bus.published[0].payload == {"_raw": "x" * 200}


async def test_poll_empty_and_null_payload_become_empty_dict(db):
    _insert(db, "t", "src", "")
    _insert(db, "t", "src", None)
    bus = FakeBus()
    await dd.DurableEventDispatcher(bus)._poll()
    assert [e.payload for e in bus.published] == [{}, {}]


# --- _poll: cursor ilerletme ---------------------------------------------------


async def test_poll_advances_and_persists_cursor(db):
    for _ in range(3):
        _insert(db, "t", "src", "{}")
    d = dd.DurableEventDispatcher(FakeBus())
    await d._poll()
    assert d.cursor == 3
    assert _cursor_rows(db) == [(1, 3)]


async def test_poll_without_rows_does_not_touch_cursor(db):
    """Bos turda yazma olmamali — 1/sn calisan dongude gereksiz WAL yazmasi (#1676)."""
    d = dd.DurableEventDispatcher(FakeBus())
    d.cursor = 5
    await d._poll()
    assert _cursor_rows(db) == []


async def test_second_poll_does_not_republish(db):
    _insert(db, "t", "src", "{}")
    bus = FakeBus()
    d = dd.DurableEventDispatcher(bus)
    await d._poll()
    await d._poll()
    assert bus.publish_calls == 1


# --- KAYIP EVENT: bulgu #1686/#1687 (mevcut davranis capalanir) ----------------


async def test_publish_failure_still_advances_cursor_event_lost(db, caplog):
    """MEVCUT DAVRANIS — bulgu #1686/#1687: publish patlarsa event kaybolur.

    Cursor basarisiz event'in uzerinden gecer, sonraki tur onu bir daha getirmez.
    Bu test dogru davranisi degil, BUGUNKUNU capalar: kapi 09-10'da acilmadan once
    davranis degistirilirse bu test kirilir ve karar bilincli alinmis olur.
    Modulun amaci "kayip-event koprusu" oldugu icin bu sessiz kayip mimari ile celisir.
    """
    _insert(db, "t", "src", "{}")  # id=1, publish PATLAR
    _insert(db, "t", "src", "{}")  # id=2, publish gecer
    bus = FakeBus(fail_ids={1})
    d = dd.DurableEventDispatcher(bus)

    with caplog.at_level("WARNING"):
        await d._poll()

    assert [e.id for e in bus.published] == [2], "basarili event yayinlanmali"
    assert d.cursor == 2, "cursor basarisiz event'in uzerinden gecti (mevcut davranis)"
    assert _cursor_rows(db) == [(1, 2)]
    assert any("publish failed" in r.getMessage() for r in caplog.records), "kayip en azindan WARNING'e dusmeli"

    # Kanit: ikinci tur kaybolan event'i GERI GETIRMEZ.
    await d._poll()
    assert [e.id for e in bus.published] == [2]


# --- _run dongusu / start / stop ----------------------------------------------


async def test_run_loop_survives_poll_exception(db, monkeypatch):
    """Tek turdaki hata dongude olmemeli — poll patlasa da dongu devam eder."""
    monkeypatch.setattr(dd, "_POLL_INTERVAL", 0.01)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("poll patladi")

    d = dd.DurableEventDispatcher(FakeBus())
    monkeypatch.setattr(d, "_poll", boom)
    task = asyncio.create_task(d._run())
    await asyncio.sleep(0.05)
    d._stopping = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls["n"] >= 2, "hata sonrasi dongu devam etmeli"


async def test_start_loads_cursor_and_stop_cancels_task(db, monkeypatch):
    monkeypatch.setattr(dd, "_POLL_INTERVAL", 0.01)
    dd.DurableEventDispatcher(FakeBus())._save_cursor(11)

    d = dd.DurableEventDispatcher(FakeBus())
    await d.start()
    assert d.cursor == 11
    assert d._task is not None
    await d.stop()
    assert d._task is None
    assert d._stopping is True


async def test_stop_without_start_is_noop():
    await dd.DurableEventDispatcher(FakeBus()).stop()


def test_create_dispatcher_returns_bound_instance():
    bus = FakeBus()
    d = dd.create_dispatcher(bus)
    assert isinstance(d, dd.DurableEventDispatcher)
    assert d.bus is bus
