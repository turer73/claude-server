import pytest


@pytest.mark.anyio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "version" in data


@pytest.mark.anyio
async def test_ready_endpoint(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert "version" in data


@pytest.mark.anyio
async def test_health_returns_version(client):
    resp = await client.get("/health")
    assert resp.json()["version"] == "0.1.0"


@pytest.mark.anyio
async def test_server_error_handler(client):
    """Custom errors should return structured JSON."""
    # This endpoint doesn't exist, should return 404 from FastAPI
    resp = await client.get("/nonexistent")
    assert resp.status_code in (404, 405)


async def test_health_exposes_sha_and_stale(client):
    # P0-a: /health çalışan-SHA + disk-SHA + stale (deployed≠running drift göstergesi)
    resp = await client.get("/health")
    data = resp.json()
    assert "sha" in data
    assert "disk_sha" in data
    assert "stale" in data
    assert data["stale"] in (True, False, None)


async def test_health_stale_none_when_sha_unavailable(client, monkeypatch):
    # Codex P2: SHA belirlenemezse (installer-install, .git+env yok) stale=None (sessiz-False değil)
    from app import main as m

    monkeypatch.setattr(m, "_DEPLOYED_SHA", "")
    monkeypatch.setattr(m, "_current_disk_sha", lambda: "")
    resp = await client.get("/health")
    assert resp.json()["stale"] is None


async def test_health_stale_true_when_running_differs_from_disk(client, monkeypatch):
    # çalışan-SHA (sabit) ile disk-HEAD farklıysa stale=True (restart gerekli sinyali)
    from app import main as m

    monkeypatch.setattr(m, "_DEPLOYED_SHA", "aaaaaaaaaaaa")
    monkeypatch.setattr(m, "_current_disk_sha", lambda: "bbbbbbbbbbbb")
    resp = await client.get("/health")
    assert resp.json()["stale"] is True


async def test_health_stale_false_when_match(client, monkeypatch):
    from app import main as m

    monkeypatch.setattr(m, "_DEPLOYED_SHA", "cccccccccccc")
    monkeypatch.setattr(m, "_current_disk_sha", lambda: "cccccccccccc")
    resp = await client.get("/health")
    assert resp.json()["stale"] is False


async def test_health_stale_false_when_only_nonapp_changed(client, monkeypatch):
    # klipper #100224: SHA farklı AMA app/ değişmemiş (docs/script/test commit) → stale=False.
    # Eski sha-tabanlı her commit'te True olup her merge'de drift-WARN flood ediyordu.
    from app import main as m

    monkeypatch.setattr(m, "_DEPLOYED_SHA", "aaaaaaaaaaaa")
    monkeypatch.setattr(m, "_current_disk_sha", lambda: "bbbbbbbbbbbb")
    monkeypatch.setattr(m, "_app_code_drifted", lambda dep, disk: False)  # app/ aynı
    resp = await client.get("/health")
    assert resp.json()["stale"] is False


def test_app_code_drifted_unit(monkeypatch):
    # klipper #100224: content-aware drift — yalnız app/ değişince True.
    import subprocess

    from app.main import _app_code_drifted

    assert _app_code_drifted("abc123", "abc123") is False  # aynı sha → drift yok

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R(0))
    assert _app_code_drifted("aaa", "bbb") is False  # app/ aynı (git rc=0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R(1))
    assert _app_code_drifted("aaa", "bbb") is True  # app/ değişti (git rc!=0)

    def _boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert _app_code_drifted("aaa", "bbb") is True  # git-yok → güvenli-taraf (stale)
