"""Tests for app/api/csp.py — CSP violation reporting + dedup."""

from __future__ import annotations

import sqlite3

import pytest

CSP_SCHEMA = """
CREATE TABLE csp_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    directive TEXT NOT NULL,
    blocked_uri TEXT NOT NULL,
    source_file TEXT,
    disposition TEXT DEFAULT 'enforce',
    user_agent TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    hit_count INTEGER DEFAULT 1,
    resolved INTEGER DEFAULT 0,
    UNIQUE(site, directive, blocked_uri)
);
"""

HEADERS = {"X-Memory-Key": "test-csp-key"}


@pytest.fixture
def csp_db(tmp_path, monkeypatch):
    """Per-test SQLite DB with the csp schema, with auth key set to a known value."""
    db_path = tmp_path / "csp.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(CSP_SCHEMA)
    conn.commit()
    conn.close()

    from app.api import csp as csp_module

    monkeypatch.setattr(csp_module, "DB_PATH", str(db_path))
    monkeypatch.setattr(csp_module, "MEMORY_API_KEY", "test-csp-key")
    return db_path


async def test_receive_violations_new(client, csp_db):
    body = {
        "violations": [
            {"site": "example.com", "directive": "script-src", "blocked_uri": "https://evil.com/x.js"},
            {"site": "example.com", "directive": "img-src", "blocked_uri": "https://other.com/y.png"},
        ]
    }
    resp = await client.post("/api/v1/csp/report", json=body, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


async def test_receive_violations_dedup(client, csp_db):
    body1 = {"violations": [{"site": "a.com", "directive": "script-src", "blocked_uri": "x.js", "hit_count": 1}]}
    await client.post("/api/v1/csp/report", json=body1, headers=HEADERS)

    body2 = {"violations": [{"site": "a.com", "directive": "script-src", "blocked_uri": "x.js", "hit_count": 5}]}
    resp = await client.post("/api/v1/csp/report", json=body2, headers=HEADERS)
    assert resp.status_code == 200

    db = sqlite3.connect(csp_db)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM csp_violations").fetchall()
    db.close()
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 6


async def test_receive_violations_requires_key(client, csp_db):
    body = {"violations": []}
    resp = await client.post("/api/v1/csp/report", json=body, headers={"X-Memory-Key": "wrong"})
    assert resp.status_code == 401


async def test_list_violations_filters(client, csp_db):
    payload = {
        "violations": [
            {"site": "alpha.com", "directive": "script-src", "blocked_uri": "a.js"},
            {"site": "beta.com", "directive": "img-src", "blocked_uri": "b.png"},
        ]
    }
    await client.post("/api/v1/csp/report", json=payload, headers=HEADERS)

    resp = await client.get("/api/v1/csp/violations", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await client.get("/api/v1/csp/violations?site=alpha.com", headers=HEADERS)
    assert len(resp.json()) == 1
    assert resp.json()[0]["site"] == "alpha.com"

    resp = await client.get("/api/v1/csp/violations?resolved=0", headers=HEADERS)
    assert len(resp.json()) == 2


async def test_list_violations_requires_key(client, csp_db):
    resp = await client.get("/api/v1/csp/violations", headers={"X-Memory-Key": "nope"})
    assert resp.status_code == 401


async def test_summary_groups_by_site(client, csp_db):
    payload = {
        "violations": [
            {"site": "alpha.com", "directive": "script-src", "blocked_uri": "a.js", "hit_count": 3},
            {"site": "alpha.com", "directive": "img-src", "blocked_uri": "x.png", "hit_count": 1},
            {"site": "beta.com", "directive": "script-src", "blocked_uri": "b.js", "hit_count": 5},
        ]
    }
    await client.post("/api/v1/csp/report", json=payload, headers=HEADERS)

    resp = await client.get("/api/v1/csp/summary", headers=HEADERS)
    assert resp.status_code == 200
    by_site = {r["site"]: r for r in resp.json()}
    assert by_site["alpha.com"]["unique_violations"] == 2
    assert by_site["alpha.com"]["total_hits"] == 4
    assert by_site["beta.com"]["unique_violations"] == 1


async def test_summary_requires_key(client, csp_db):
    resp = await client.get("/api/v1/csp/summary", headers={"X-Memory-Key": "wrong"})
    assert resp.status_code == 401


async def test_resolve_violation(client, csp_db):
    payload = {"violations": [{"site": "x.com", "directive": "script-src", "blocked_uri": "z.js"}]}
    await client.post("/api/v1/csp/report", json=payload, headers=HEADERS)
    db = sqlite3.connect(csp_db)
    vid = db.execute("SELECT id FROM csp_violations").fetchone()[0]
    db.close()

    resp = await client.post(f"/api/v1/csp/resolve/{vid}", headers=HEADERS)
    assert resp.status_code == 200

    db = sqlite3.connect(csp_db)
    resolved = db.execute("SELECT resolved FROM csp_violations WHERE id=?", (vid,)).fetchone()[0]
    db.close()
    assert resolved == 1


async def test_resolve_missing_violation_404(client, csp_db):
    resp = await client.post("/api/v1/csp/resolve/99999", headers=HEADERS)
    assert resp.status_code == 404


async def test_resolve_requires_key(client, csp_db):
    resp = await client.post("/api/v1/csp/resolve/1", headers={"X-Memory-Key": "wrong"})
    assert resp.status_code == 401
