"""SQLite connection + schema bootstrap helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator


def bootstrap_schema(db_path: str | Path) -> None:
    """Create polymem tables + FTS indexes if they don't exist. Idempotent.

    Re-runs an FTS5 rebuild so existing rows (e.g. if a DB was created before
    FTS was added) are searchable. Cheap on small/empty DBs.
    """
    schema_sql = files("polymem").joinpath("schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(schema_sql)
        for table in ("memories_fts", "sessions_fts"):
            conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        conn.commit()


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with sane defaults (row_factory, foreign keys)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()
