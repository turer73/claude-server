"""Tests for CI lesson_learned DB schema (distinct from Pydantic schemas)."""

import pytest


@pytest.mark.asyncio
async def test_ci_lesson_learned_table_exists(ci_db):
    cursor = await ci_db.conn.execute("PRAGMA table_info(ci_lesson_learned)")
    rows = await cursor.fetchall()
    cols = {row["name"]: row["type"] for row in rows}
    assert cols == {
        "id": "INTEGER",
        "run_uuid": "TEXT",
        "project": "TEXT",
        "test_name": "TEXT",
        "error_hash": "TEXT",
        "signature": "TEXT",
        "raw_error": "TEXT",
        "attempt_num": "INTEGER",
        "strategy": "TEXT",
        "context_lessons": "TEXT",
        "fix_diff": "TEXT",
        "outcome": "TEXT",
        "duration_ms": "INTEGER",
        "created_at": "TEXT",
    }


@pytest.mark.asyncio
async def test_ci_lesson_learned_indexes_exist(ci_db):
    cursor = await ci_db.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ci_lesson_learned'")
    rows = await cursor.fetchall()
    names = {r["name"] for r in rows}
    assert "idx_lesson_signature" in names
    assert "idx_lesson_project" in names
    assert "idx_lesson_run_uuid" in names
