"""Database._add_column — iki worker ayni anda ALTER atinca cokmemeli.

Repro: 2 uvicorn worker fresh bir server.db uzerinde ayni anda `_migrate()`
kosuyor. Ikisi de `PRAGMA table_info` ile kolonu YOK goruyor, ikisi de
`ALTER TABLE ... ADD COLUMN` atiyor; kaybeden worker
`sqlite3.OperationalError: duplicate column name: X` aliyor ve startup patliyor.
2026-08-15'te server.db sifirdan kurulunca (7. bozulma, disc#1462) tam bu yol
tetiklendi.

Kontrat: "duplicate column name" IDEMPOTENT kabul edilir (kolon zaten var =
istenen son durum). BASKA hicbir OperationalError yutulmaz — aksi halde gercek
sema hatalari sessizce gizlenir ve tablo eksik kalirdi.

Not: `_migrate()`in tekrar-kosulabilirligi zaten
tests/test_interv_rollback.py::test_remediation_log_migration_idempotent
tarafindan kapsaniyor; burada tekrarlanmiyor.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.database import Database


async def test_duplicate_column_is_idempotent(tmp_path: Path) -> None:
    """Ayni kolonu iki kez eklemek — ikinci ALTER sessizce yutulmali."""
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        await db.conn.execute("CREATE TABLE race (id INTEGER)")

        await db._add_column("race", "flag", "INTEGER NOT NULL DEFAULT 0")
        # Kaybeden worker'in attigi ikinci ALTER — patlamamali.
        await db._add_column("race", "flag", "INTEGER NOT NULL DEFAULT 0")

        cur = await db.conn.execute("PRAGMA table_info(race)")
        cols = {row[1] for row in await cur.fetchall()}
        assert cols == {"id", "flag"}
    finally:
        await db.close()


async def test_other_operational_errors_still_raise(tmp_path: Path) -> None:
    """Yutma SADECE duplicate-column icin — olmayan tablo hala patlamali.

    Bu test olmadan `except OperationalError: pass`'e kayma riski var; o zaman
    gercek sema hatalari sessizce yutulur ve eksik tablo fark edilmez.
    """
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    try:
        with pytest.raises(sqlite3.OperationalError) as exc:
            await db._add_column("boyle_bir_tablo_yok", "x", "TEXT")
        assert "duplicate column name" not in str(exc.value)
    finally:
        await db.close()
