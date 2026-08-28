"""Database._with_recovery — zehirlenmis kalici baglantiyi tazele, digerlerine DOKUNMA.

Repro (2026-08-18 03:37 -> 08-26 06:51, 8 gun 3 saat): uygulamanin kalici aiosqlite
baglantisi `sqlite3.DatabaseError: file is not a database` atmaya basladi ve bir daha
duzelmedi -> 55.855 hata, metrics ~45k / events ~93k / audit_log ~18k satir kayip.
Dosya yazilabilir durumdaydi: cron'un kisa-omurlu sqlite3 CLI'i ayni dosyaya 8 gun
boyunca sorunsuz yazdi (cron_outcomes 2026 satir/gun, kesintisiz). Yani ariza
DOSYA'da degil BAGLANTI'daydi ve tek care yeniden baglanmakti.

Kontrat:
  - "file is not a database" / "malformed" -> BIR KEZ reconnect + retry.
  - IntegrityError (kisit ihlali), "no such table" (sema), "database is locked"
    (busy_timeout'un isi) -> reconnect YOK, hata oldugu gibi yukari.
  - Reconnect da basarisizsa -> bant-disi eskalasyon (report_db_failure) + raise.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.db.database import Database


def _poison_once(db: Database, message: str) -> dict[str, int]:
    """Mevcut baglantinin execute'unu BIR KEZ patlat. Reconnect yeni baglanti
    acacagi icin ikinci deneme kendiliginden saglam baglantiya duser."""
    calls = {"n": 0}
    original = db._conn.execute

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.DatabaseError(message)
        return await original(*args, **kwargs)

    db._conn.execute = flaky  # type: ignore[method-assign]
    return calls


@pytest.mark.parametrize("message", ["file is not a database", "database disk image is malformed"])
async def test_poisoned_connection_reconnects_and_succeeds(tmp_path: Path, message: str) -> None:
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        gen_before = db._generation
        _poison_once(db, message)

        # Zehirli baglantiya ragmen yazim BASARILI olmali.
        await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", (1, "a"))

        assert db._generation == gen_before + 1, "reconnect olmali"
        rows = await db.fetch_all("SELECT id, v FROM t")
        assert rows == [{"id": 1, "v": "a"}]
    finally:
        await db.close()


async def test_write_is_not_duplicated_by_retry(tmp_path: Path) -> None:
    """Hata commit'ten ONCE atiliyor -> retry yazmayi CIFTLEMEZ."""
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.execute("CREATE TABLE t (id INTEGER)")
        _poison_once(db, "file is not a database")
        await db.execute("INSERT INTO t (id) VALUES (1)")

        rows = await db.fetch_all("SELECT id FROM t")
        assert rows == [{"id": 1}], "tek satir olmali, retry ciftlememeli"
    finally:
        await db.close()


async def test_fetch_paths_also_recover(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.execute("CREATE TABLE t (id INTEGER)")
        await db.execute("INSERT INTO t (id) VALUES (7)")

        _poison_once(db, "file is not a database")
        assert await db.fetch_all("SELECT id FROM t") == [{"id": 7}]

        _poison_once(db, "file is not a database")
        assert await db.fetch_one("SELECT id FROM t") == {"id": 7}
    finally:
        await db.close()


async def test_integrity_error_is_not_retried(tmp_path: Path) -> None:
    """Kisit ihlali gercek bir bug'dir; reconnect onu maskelememeli."""
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        await db.execute("INSERT INTO t (id) VALUES (1)")
        gen_before = db._generation

        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO t (id) VALUES (1)")

        assert db._generation == gen_before, "IntegrityError'da reconnect OLMAMALI"
    finally:
        await db.close()


async def test_schema_error_is_not_retried(tmp_path: Path) -> None:
    """'no such table' sema hatasi — reconnect faydasiz, sessizce yutulmamali."""
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        gen_before = db._generation
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await db.fetch_all("SELECT * FROM yok_boyle_tablo")
        assert db._generation == gen_before
    finally:
        await db.close()


async def test_reconnect_failure_escalates_out_of_band(tmp_path: Path, monkeypatch: Any) -> None:
    """Yeniden baglanma da coktuyse SESSIZ KALMA — bant-disi alarm sart."""
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.execute("CREATE TABLE t (id INTEGER)")

        reported: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "app.db.database.report_db_failure",
            lambda ctx, err: reported.append((ctx, str(err))),
        )

        async def broken_open() -> Any:
            raise OSError("disk gitti")

        monkeypatch.setattr(db, "_open", broken_open)
        _poison_once(db, "file is not a database")

        with pytest.raises(sqlite3.DatabaseError):
            await db.execute("INSERT INTO t (id) VALUES (1)")

        assert reported, "reconnect basarisizken bant-disi alarm cagrilmali"
        assert "disk gitti" in reported[0][1]
    finally:
        db._conn = None  # kirik durumda close() denemesin


async def test_recovery_reports_recovered(tmp_path: Path, monkeypatch: Any) -> None:
    """Kurtarma basarili olduysa 'duzeldi' de bildirilmeli (epizod kapansin)."""
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.execute("CREATE TABLE t (id INTEGER)")
        recovered: list[str] = []
        monkeypatch.setattr("app.db.database.report_db_recovered", lambda ctx: recovered.append(ctx))

        _poison_once(db, "file is not a database")
        await db.execute("INSERT INTO t (id) VALUES (1)")

        assert recovered == ["execute"]
    finally:
        await db.close()


async def test_concurrent_poisoned_calls_reconnect_only_once(tmp_path: Path) -> None:
    """N es zamanli cagri ayni arizayi gorse de TEK reconnect olmali.

    Aksi halde 55.855 sessiz hatanin yerini 55.855 reconnect firtinasi alirdi.
    """
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.execute("CREATE TABLE t (id INTEGER)")
        gen_before = db._generation

        original = db._conn.execute
        failed: dict[str, int] = {"n": 0}

        async def flaky(*args: Any, **kwargs: Any) -> Any:
            # Eski baglantiya gelen HER cagri patlar; reconnect sonrasi yeni
            # baglanti nesnesi kullanildigi icin retry'lar saglam yola duser.
            failed["n"] += 1
            raise sqlite3.DatabaseError("file is not a database")

        db._conn.execute = flaky  # type: ignore[method-assign]

        await asyncio.gather(*[db.execute("INSERT INTO t (id) VALUES (?)", (i,)) for i in range(8)])

        assert db._generation == gen_before + 1, f"tek reconnect beklenir, jenerasyon={db._generation}"
        rows = await db.fetch_all("SELECT id FROM t ORDER BY id")
        assert [r["id"] for r in rows] == list(range(8)), "8 yazimin hepsi tamamlanmali"
    finally:
        await db.close()
