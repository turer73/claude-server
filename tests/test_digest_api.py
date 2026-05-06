"""Tests for app/api/digest.py — JSON shape + auth + NOTHING_NEW guard.

These tests stub `app.core.digest.gather` and friends so the API layer
is exercised in isolation; the gather logic is tested separately when
the underlying data sources change.
"""

from __future__ import annotations

import pytest

from app.core import digest as core_digest


def _stub_gather(monkeypatch, *, has_signal: bool):
    """Replace gather/has_signal with deterministic fakes."""

    fake_data = {
        "memory": {
            "open_bugs": [{"id": 1, "project": "x", "title": "stub"}],
            "new_bugs": [],
            "unread_notes": [],
        },
        "commits": {"x": []},
        "cron": {"self_pentest": None},
        "system": {
            "service": "active",
            "disk_used_pct": "10%",
            "disk_avail": "9G",
            "mem_used_mb": "100",
            "mem_total_mb": "8000",
        },
    }

    monkeypatch.setattr(core_digest, "gather", lambda token=None: fake_data)
    monkeypatch.setattr(core_digest, "has_signal", lambda d: has_signal)
    monkeypatch.setattr(core_digest, "load_env", lambda: {})


@pytest.mark.anyio
async def test_digest_data_requires_auth(client):
    r = await client.get("/api/v1/digest/data")
    assert r.status_code in (401, 403)


@pytest.mark.anyio
async def test_digest_data_returns_shape(client, auth_headers, monkeypatch):
    _stub_gather(monkeypatch, has_signal=True)
    r = await client.get("/api/v1/digest/data", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"memory", "commits", "cron", "system", "has_signal"}
    assert body["has_signal"] is True
    assert body["memory"]["open_bugs"][0]["title"] == "stub"


@pytest.mark.anyio
async def test_digest_send_skips_when_no_signal(client, auth_headers, monkeypatch):
    _stub_gather(monkeypatch, has_signal=False)
    sends: list[bool] = []
    monkeypatch.setattr(core_digest, "send_telegram", lambda *a, **kw: sends.append(True) or True)

    r = await client.post("/api/v1/digest/send", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body == {"sent": False, "reason": "NOTHING_NEW"}
    assert sends == []  # send_telegram was NOT invoked
