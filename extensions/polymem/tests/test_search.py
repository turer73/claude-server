"""FTS5 search tests."""
from __future__ import annotations


def _mem(client, auth_headers, **overrides):
    payload = {
        "type": "feedback",
        "name": "default",
        "description": "default desc",
        "content": "default content",
    }
    payload.update(overrides)
    r = client.post("/memories", headers=auth_headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _sess(client, auth_headers, **overrides):
    payload = {"summary": "default session"}
    payload.update(overrides)
    r = client.post("/sessions", headers=auth_headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ----- auth + validation -----

def test_search_auth_required(client):
    r = client.get("/search?q=foo")
    assert r.status_code == 401


def test_search_rejects_short_query(client, auth_headers):
    r = client.get("/search?q=a", headers=auth_headers)
    assert r.status_code == 422


def test_search_punctuation_only_returns_empty(client, auth_headers):
    _mem(client, auth_headers, name="hit", content="kernel module rebuild")
    r = client.get("/search?q=---", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0


# ----- core matching -----

def test_search_matches_memory_content(client, auth_headers):
    _mem(client, auth_headers, name="hit", content="kernel module rebuild ritual")
    _mem(client, auth_headers, name="miss", content="something else entirely")
    body = client.get("/search?q=kernel", headers=auth_headers).json()
    names = [h["name"] for h in body["results"]["memories"]]
    assert "hit" in names and "miss" not in names


def test_search_matches_memory_name(client, auth_headers):
    _mem(client, auth_headers, name="oauth-race-fix", content="x")
    body = client.get("/search?q=oauth", headers=auth_headers).json()
    assert any(h["name"] == "oauth-race-fix" for h in body["results"]["memories"])


def test_search_prefix_match(client, auth_headers):
    _mem(client, auth_headers, name="hit", content="kubernetes operator")
    body = client.get("/search?q=kuber", headers=auth_headers).json()
    assert any(h["name"] == "hit" for h in body["results"]["memories"])


def test_search_matches_session_summary(client, auth_headers):
    _sess(client, auth_headers, summary="deployed kuafor v2 patch")
    _sess(client, auth_headers, summary="unrelated work")
    body = client.get("/search?q=kuafor", headers=auth_headers).json()
    summaries = [h["snippet"] for h in body["results"]["sessions"]]
    assert any("kuafor" in s.lower() for s in summaries)


def test_search_returns_snippet_with_highlight(client, auth_headers):
    _mem(client, auth_headers, name="x", content="the panola erp migration broke ingest")
    body = client.get("/search?q=panola", headers=auth_headers).json()
    snip = body["results"]["memories"][0]["snippet"]
    assert "<b>panola</b>" in snip.lower() or "<b>Panola</b>" in snip


# ----- filtering + lifecycle -----

def test_search_excludes_soft_deleted(client, auth_headers):
    m = _mem(client, auth_headers, name="trashed", content="rare-token-zxq")
    client.delete(f"/memories/{m['id']}", headers=auth_headers)
    body = client.get("/search?q=rare-token-zxq", headers=auth_headers).json()
    assert body["results"]["memories"] == []


def test_search_reflects_update(client, auth_headers):
    m = _mem(client, auth_headers, name="updated", content="initialvalue")
    body = client.get("/search?q=initialvalue", headers=auth_headers).json()
    assert any(h["id"] == m["id"] for h in body["results"]["memories"])

    client.put(f"/memories/{m['id']}", headers=auth_headers, json={"content": "swappedvalue"})
    body = client.get("/search?q=initialvalue", headers=auth_headers).json()
    assert all(h["id"] != m["id"] for h in body["results"]["memories"])
    body2 = client.get("/search?q=swappedvalue", headers=auth_headers).json()
    assert any(h["id"] == m["id"] for h in body2["results"]["memories"])


def test_search_reflects_session_delete(client, auth_headers):
    s = _sess(client, auth_headers, summary="rare-zzqq-summary")
    body = client.get("/search?q=rare-zzqq-summary", headers=auth_headers).json()
    assert any(h["id"] == s["id"] for h in body["results"]["sessions"])
    client.delete(f"/sessions/{s['id']}", headers=auth_headers)
    body = client.get("/search?q=rare-zzqq-summary", headers=auth_headers).json()
    assert body["results"]["sessions"] == []


# ----- limit + total -----

def test_search_limit_caps_per_kind(client, auth_headers):
    for i in range(5):
        _mem(client, auth_headers, name=f"m{i}", content="bulkquery shared")
    body = client.get("/search?q=bulkquery&limit=2", headers=auth_headers).json()
    assert len(body["results"]["memories"]) == 2
    assert body["total"] == 2


def test_search_total_sums_kinds(client, auth_headers):
    _mem(client, auth_headers, name="m", content="crosshit token")
    _sess(client, auth_headers, summary="crosshit token in session")
    body = client.get("/search?q=crosshit", headers=auth_headers).json()
    assert body["total"] == 2
    assert len(body["results"]["memories"]) == 1
    assert len(body["results"]["sessions"]) == 1
