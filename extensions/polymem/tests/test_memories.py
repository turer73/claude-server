"""Memories CRUD + auth tests."""

from __future__ import annotations


def _make(client, auth_headers, **overrides):
    payload = {
        "type": "user",
        "name": "test-mem",
        "description": "a test memory",
        "content": "the actual content goes here",
    }
    payload.update(overrides)
    r = client.post("/memories", headers=auth_headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ----- auth -----


def test_auth_missing_key_rejects(client):
    r = client.get("/memories")
    assert r.status_code == 401


def test_auth_wrong_key_rejects(client):
    r = client.get("/memories", headers={"X-Memory-Key": "nope"})
    assert r.status_code == 401


def test_auth_correct_key_ok(client, auth_headers):
    r = client.get("/memories", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []


def test_auth_disabled_mode_skips_check(client_noauth):
    r = client_noauth.get("/memories")
    assert r.status_code == 200


# ----- create -----


def test_create_minimal_fields(client, auth_headers):
    m = _make(client, auth_headers)
    assert m["id"] >= 1
    assert m["active"] == 1
    assert m["read_count"] == 0
    assert m["source_device"] is None
    assert m["rationale"] is None
    assert m["created_at"] and m["updated_at"]


def test_create_with_source_device(client, auth_headers):
    m = _make(client, auth_headers, source_device="laptop-1", rationale="captured during demo")
    assert m["source_device"] == "laptop-1"
    assert m["rationale"] == "captured during demo"


def test_create_rejects_bad_type(client, auth_headers):
    r = client.post(
        "/memories",
        headers=auth_headers,
        json={"type": "bogus", "name": "x", "description": "y", "content": "z"},
    )
    assert r.status_code == 422


# ----- list + filter -----


def test_list_filters_by_type(client, auth_headers):
    _make(client, auth_headers, type="user", name="u1")
    _make(client, auth_headers, type="feedback", name="f1")
    _make(client, auth_headers, type="feedback", name="f2")
    r = client.get("/memories?type=feedback", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(m["type"] == "feedback" for m in rows)


def test_list_filters_by_device(client, auth_headers):
    _make(client, auth_headers, source_device="a")
    _make(client, auth_headers, source_device="b")
    r = client.get("/memories?device=a", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["source_device"] == "a"


def test_list_excludes_inactive_by_default(client, auth_headers):
    m = _make(client, auth_headers)
    client.delete(f"/memories/{m['id']}", headers=auth_headers)
    r = client.get("/memories", headers=auth_headers)
    assert r.json() == []
    r2 = client.get("/memories?active=0", headers=auth_headers)
    assert len(r2.json()) == 1


# ----- read by id -----


def test_get_by_id(client, auth_headers):
    m = _make(client, auth_headers)
    r = client.get(f"/memories/{m['id']}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["id"] == m["id"]


def test_get_missing_returns_404(client, auth_headers):
    r = client.get("/memories/9999", headers=auth_headers)
    assert r.status_code == 404


# ----- update -----


def test_update_changes_fields_and_touches_updated_at(client, auth_headers):
    m = _make(client, auth_headers)
    r = client.put(f"/memories/{m['id']}", headers=auth_headers, json={"content": "new content"})
    assert r.status_code == 200
    assert r.json()["content"] == "new content"
    # updated_at is set via datetime('now') — at minimum it returns a non-empty string
    assert r.json()["updated_at"]


def test_update_empty_body_rejected(client, auth_headers):
    m = _make(client, auth_headers)
    r = client.put(f"/memories/{m['id']}", headers=auth_headers, json={})
    assert r.status_code == 400


def test_update_missing_returns_404(client, auth_headers):
    r = client.put("/memories/9999", headers=auth_headers, json={"content": "x"})
    assert r.status_code == 404


# ----- soft delete -----


def test_soft_delete_marks_inactive(client, auth_headers):
    m = _make(client, auth_headers)
    r = client.delete(f"/memories/{m['id']}", headers=auth_headers)
    assert r.status_code == 200
    again = client.get(f"/memories/{m['id']}", headers=auth_headers)
    assert again.status_code == 200
    assert again.json()["active"] == 0


# ----- mark read -----


def test_mark_read_increments_counter(client, auth_headers):
    m = _make(client, auth_headers)
    r = client.put(f"/memories/{m['id']}/read", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["read_count"] == 1
    assert r.json()["last_read_at"] is not None
    # Idempotent counter — second call goes to 2
    r2 = client.put(f"/memories/{m['id']}/read", headers=auth_headers)
    assert r2.json()["read_count"] == 2


def test_mark_read_missing_returns_404(client, auth_headers):
    r = client.put("/memories/9999/read", headers=auth_headers)
    assert r.status_code == 404
