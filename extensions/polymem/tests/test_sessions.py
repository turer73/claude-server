"""Sessions tests."""

from __future__ import annotations


def _create(client, auth_headers, **overrides):
    payload = {"summary": "did stuff"}
    payload.update(overrides)
    r = client.post("/sessions", headers=auth_headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ----- auth -----


def test_sessions_auth_required(client):
    r = client.get("/sessions")
    assert r.status_code == 401


def test_sessions_auth_disabled_mode_ok(client_noauth):
    r = client_noauth.get("/sessions")
    assert r.status_code == 200
    assert r.json() == []


# ----- create -----


def test_create_minimal_defaults_date_today(client, auth_headers):
    s = _create(client, auth_headers)
    assert s["summary"] == "did stuff"
    assert s["device_name"] is None
    assert s["project"] is None
    # Date defaulted to today at DB level — non-empty ISO.
    assert s["date"] and len(s["date"]) == 10


def test_create_rejects_empty_summary(client, auth_headers):
    r = client.post("/sessions", headers=auth_headers, json={"summary": ""})
    assert r.status_code == 422


def test_create_persists_metadata_roundtrip(client, auth_headers):
    meta = {"files_changed": 3, "tasks": ["a", "b"], "nested": {"k": 1}}
    s = _create(client, auth_headers, metadata=meta)
    assert s["metadata"] == meta
    # Re-fetch — make sure JSON survived a write+read cycle.
    r = client.get(f"/sessions/{s['id']}", headers=auth_headers)
    assert r.json()["metadata"] == meta


def test_create_with_explicit_date_and_context(client, auth_headers):
    s = _create(
        client,
        auth_headers,
        summary="extended",
        device_name="klipper",
        project="polymem",
        date="2026-05-28",
    )
    assert s["date"] == "2026-05-28"
    assert s["device_name"] == "klipper"
    assert s["project"] == "polymem"


# ----- get / list -----


def test_get_missing_returns_404(client, auth_headers):
    r = client.get("/sessions/999", headers=auth_headers)
    assert r.status_code == 404


def test_list_orders_by_date_desc_then_id(client, auth_headers):
    a = _create(client, auth_headers, date="2026-05-26", summary="a")
    b = _create(client, auth_headers, date="2026-05-28", summary="b")
    c = _create(client, auth_headers, date="2026-05-27", summary="c")
    rows = client.get("/sessions", headers=auth_headers).json()
    assert [r["summary"] for r in rows] == ["b", "c", "a"]
    assert {a["id"], b["id"], c["id"]} == {r["id"] for r in rows}


def test_list_filters_by_device(client, auth_headers):
    _create(client, auth_headers, device_name="klipper", summary="k1")
    _create(client, auth_headers, device_name="laptop", summary="l1")
    rows = client.get("/sessions?device=klipper", headers=auth_headers).json()
    assert [r["summary"] for r in rows] == ["k1"]


def test_list_filters_by_project(client, auth_headers):
    _create(client, auth_headers, project="polymem", summary="p1")
    _create(client, auth_headers, project="goose", summary="g1")
    rows = client.get("/sessions?project=polymem", headers=auth_headers).json()
    assert [r["summary"] for r in rows] == ["p1"]


def test_list_filters_by_date_range(client, auth_headers):
    _create(client, auth_headers, date="2026-05-20", summary="old")
    _create(client, auth_headers, date="2026-05-25", summary="mid")
    _create(client, auth_headers, date="2026-05-30", summary="new")
    rows = client.get(
        "/sessions?date_from=2026-05-22&date_to=2026-05-28",
        headers=auth_headers,
    ).json()
    assert [r["summary"] for r in rows] == ["mid"]


def test_list_limit_caps_results(client, auth_headers):
    for i in range(5):
        _create(client, auth_headers, summary=f"s{i}")
    rows = client.get("/sessions?limit=3", headers=auth_headers).json()
    assert len(rows) == 3


def test_list_rejects_bad_limit(client, auth_headers):
    r = client.get("/sessions?limit=0", headers=auth_headers)
    assert r.status_code == 422
    r2 = client.get("/sessions?limit=10000", headers=auth_headers)
    assert r2.status_code == 422


# ----- delete -----


def test_delete_removes_session(client, auth_headers):
    s = _create(client, auth_headers)
    r = client.delete(f"/sessions/{s['id']}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert client.get(f"/sessions/{s['id']}", headers=auth_headers).status_code == 404


def test_delete_missing_returns_404(client, auth_headers):
    r = client.delete("/sessions/999", headers=auth_headers)
    assert r.status_code == 404
