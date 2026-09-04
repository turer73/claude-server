"""presence_manager testleri — kapali-kapi davranisi + kapi acildiginda gercek mantik.

Bu modul 2026-08-27'de commit edilmeden production'a girdi ve 8 gun testsiz kostu
(PR#380 ile repoya alindi). `PRESENCE_WRITES_ENABLED=False` kontrollu deney geregi
KAPALI (kesif #1676); testler kapiyi izole bicimde acip asil mantigi dogrular, boylece
kapi geri acildiginda davranis kanitli olur.

Hicbir test CANLI server.db'ye dokunmaz: `server_db_path` tmp_path'e monkeypatch edilir.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from app.core import presence_manager as pm


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Izole DB + yazma kapisi ACIK + schema-cache sifirlanmis."""
    path = str(tmp_path / "server.db")
    monkeypatch.setattr(pm, "server_db_path", lambda: path)
    monkeypatch.setattr(pm, "PRESENCE_WRITES_ENABLED", True)
    monkeypatch.setattr(pm, "_schema_ready", False)
    return path


@pytest.fixture
def closed_db(tmp_path, monkeypatch):
    """Izole DB + yazma kapisi KAPALI (uretimdeki mevcut durum)."""
    path = str(tmp_path / "server.db")
    monkeypatch.setattr(pm, "server_db_path", lambda: path)
    monkeypatch.setattr(pm, "PRESENCE_WRITES_ENABLED", False)
    monkeypatch.setattr(pm, "_schema_ready", False)
    return path


def _rows(path: str) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM agent_instances")]
    finally:
        conn.close()


def _table_exists(path: str) -> bool:
    conn = sqlite3.connect(path)
    try:
        return bool(conn.execute("SELECT name FROM sqlite_master WHERE name='agent_instances'").fetchone())
    finally:
        conn.close()


# --- kapali kapi: HICBIR yazma olmamali (deneyin kendisi) ---------------------


def test_gate_closed_writes_nothing(closed_db):
    """Kapi kapaliyken tablo bile yaratilmamali — deney 'yazma yolu kapali' iddiasi budur."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    p.heartbeat("critic", status="working")
    p.expire_leases()
    pm.ensure_schema()

    assert not _table_exists(closed_db), "kapi kapaliyken agent_instances yaratildi — deney kirik"


def test_gate_closed_mark_stopping_also_writes_nothing(closed_db):
    """mark_stopping heartbeat'e delege eder; kapi kapaliyken o da sessiz kalmali."""
    pm.AgentPresenceManager().mark_stopping("critic")
    assert not _table_exists(closed_db)


# --- upsert -------------------------------------------------------------------


def test_upsert_creates_row_with_lease(db):
    before = time.time()
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {"evaluator": True}, model="m", version="v", pid=42)

    (row,) = _rows(db)
    assert row["agent_id"] == "critic"
    assert row["status"] == "idle"
    assert row["pid"] == 42
    assert json.loads(row["capabilities"]) == {"evaluator": True}
    assert row["lease_until"] >= before + pm.LEASE_TTL
    assert row["lease_until"] == pytest.approx(row["last_heartbeat"] + pm.LEASE_TTL)


def test_upsert_is_idempotent_per_agent_and_keeps_started_at(db):
    """Restart = yeniden kayit: tek satir kalir, started_at KORUNUR (ON CONFLICT guncellemez)."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    first_started = _rows(db)[0]["started_at"]

    time.sleep(0.01)
    p.upsert("critic", "i2", "critic", "klipper", "klipper", {"x": 1})

    rows = _rows(db)
    assert len(rows) == 1, "agent_id UNIQUE degil — restart satir cogaltiyor"
    assert rows[0]["instance_id"] == "i2"
    assert rows[0]["started_at"] == first_started


def test_upsert_restart_resets_status_to_idle(db):
    """Cokmus ajan 'working' kalmis olabilir; restart onu idle'a cekmeli."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    p.heartbeat("critic", status="working")
    assert _rows(db)[0]["status"] == "working"

    p.upsert("critic", "i2", "critic", "klipper", "klipper", {})
    assert _rows(db)[0]["status"] == "idle"


def test_upsert_serializes_unjsonable_capabilities(db):
    """default=str sayesinde JSON'lanamayan deger patlatmamali (fail-safe)."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {"when": object()})

    assert len(_rows(db)) == 1


# --- heartbeat ----------------------------------------------------------------


def test_heartbeat_renews_lease(db):
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    first = _rows(db)[0]["lease_until"]

    time.sleep(0.01)
    p.heartbeat("critic")

    assert _rows(db)[0]["lease_until"] > first


def test_heartbeat_none_status_does_not_downgrade(db):
    """status=None mevcut durumu KORUMALI — 'durum gerilemesi yok' invariant'i."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    p.heartbeat("critic", status="working")

    p.heartbeat("critic")

    assert _rows(db)[0]["status"] == "working"


def test_heartbeat_revives_offline_agent(db):
    """offline satir heartbeat alirsa idle'a donmeli (lease-expire sonrasi dirilme)."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    p.expire_leases.__self__  # noqa: B018 - okunabilirlik icin no-op
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agent_instances SET status='offline'")
    conn.commit()
    conn.close()

    p.heartbeat("critic")

    assert _rows(db)[0]["status"] == "idle"


def test_heartbeat_coalesce_keeps_existing_fields(db):
    """None gecilen alanlar mevcut degeri korumali (COALESCE)."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    p.heartbeat("critic", current_task="t1", current_project="proj", last_event_id=7)

    p.heartbeat("critic")

    row = _rows(db)[0]
    assert row["current_task"] == "t1"
    assert row["current_project"] == "proj"
    assert row["last_event_id"] == 7


def test_heartbeat_unknown_agent_is_noop_not_error(db):
    """Kayitsiz agent_id icin UPDATE 0 satir etkiler — sessizce gecmeli."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})

    p.heartbeat("hayalet-ajan")

    assert len(_rows(db)) == 1


def test_mark_stopping_sets_status(db):
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})

    p.mark_stopping("critic")

    assert _rows(db)[0]["status"] == "stopping"


# --- expire_leases ------------------------------------------------------------


def test_expire_marks_stale_offline(db):
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agent_instances SET lease_until=? , status='working'", (time.time() - 1,))
    conn.commit()
    conn.close()

    p.expire_leases()

    assert _rows(db)[0]["status"] == "offline"


def test_expire_leaves_fresh_lease_alone(db):
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    p.heartbeat("critic", status="working")

    p.expire_leases()

    assert _rows(db)[0]["status"] == "working"


def test_expire_does_not_clobber_stopping(db):
    """'stopping' kasitli bir gecis durumu; expire onu offline'a EZMEMELI."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agent_instances SET lease_until=?, status='stopping'", (time.time() - 1,))
    conn.commit()
    conn.close()

    p.expire_leases()

    assert _rows(db)[0]["status"] == "stopping"


# --- okuma yollari (kapi kapaliyken de ACIK) ----------------------------------


def test_list_instances_computes_alive_and_parses_json(db):
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {"evaluator": True}, metadata={"m": 1})
    p.upsert("olu", "i2", "critic", "klipper", "klipper", {})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agent_instances SET lease_until=? WHERE agent_id='olu'", (time.time() - 1,))
    conn.commit()
    conn.close()

    by_id = {d["agent_id"]: d for d in p.list_instances()}

    assert by_id["critic"]["alive"] is True
    assert by_id["critic"]["capabilities"] == {"evaluator": True}
    assert by_id["critic"]["metadata"] == {"m": 1}
    assert by_id["olu"]["alive"] is False


def test_list_instances_survives_corrupt_json(db):
    """Bozuk JSON satiri tum listeyi dusurmemeli — _load_json bos dict doner."""
    p = pm.AgentPresenceManager()
    p.upsert("critic", "i1", "critic", "klipper", "klipper", {})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agent_instances SET capabilities='{bozuk'")
    conn.commit()
    conn.close()

    (row,) = p.list_instances()

    assert row["capabilities"] == {}


def test_list_instances_returns_empty_when_table_missing(closed_db):
    """Tablo hic yokken (kapi kapali, hic yazilmamis) patlamamali, [] donmeli."""
    assert pm.AgentPresenceManager().list_instances() == []


def test_list_alive_filters_expired(db):
    p = pm.AgentPresenceManager()
    p.upsert("canli", "i1", "critic", "klipper", "klipper", {})
    p.upsert("olu", "i2", "critic", "klipper", "klipper", {})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE agent_instances SET lease_until=? WHERE agent_id='olu'", (time.time() - 1,))
    conn.commit()
    conn.close()

    assert [d["agent_id"] for d in p.list_alive()] == ["canli"]


def test_who_is_working_on_matches_project_and_status(db):
    p = pm.AgentPresenceManager()
    p.upsert("a", "i1", "critic", "klipper", "klipper", {})
    p.upsert("b", "i2", "critic", "klipper", "klipper", {})
    p.upsert("c", "i3", "critic", "klipper", "klipper", {})
    p.heartbeat("a", status="working", current_project="linux-ai-server")
    p.heartbeat("b", status="idle", current_project="linux-ai-server")
    p.heartbeat("c", status="stopping", current_project="linux-ai-server")

    assert sorted(d["agent_id"] for d in p.who_is_working_on("linux-ai-server")) == ["a", "b"]


def test_who_is_working_on_ignores_other_projects(db):
    p = pm.AgentPresenceManager()
    p.upsert("a", "i1", "critic", "klipper", "klipper", {})
    p.heartbeat("a", status="working", current_project="baska-proje")

    assert p.who_is_working_on("linux-ai-server") == []


# --- fail-safe ----------------------------------------------------------------


@pytest.mark.parametrize(("method", "args"), [("upsert", ("a", "i", "t", "h", "d", {})), ("heartbeat", ("a",)), ("expire_leases", ())])
def test_db_failure_is_logged_not_raised(db, monkeypatch, caplog, method, args):
    """DB patlarsa ajan start()/loop'u DUSMEMELI — sadece warning."""
    monkeypatch.setattr(pm, "_schema_ready", True)  # ensure_schema'yi atla, hatayi asil cagriya dusur

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(pm, "get_conn", boom)

    with caplog.at_level("WARNING"):
        getattr(pm.AgentPresenceManager(), method)(*args)

    assert any("failed" in r.message or "failed" in r.getMessage() for r in caplog.records)
