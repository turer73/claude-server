"""docker_containers_liveness testleri (app/core/liveness.py).

2026-06-12 incident gate'i: reboot sonrası konteyner 'Up (healthy)' görünürken
network/port-binding'siz kalabiliyor (grafana/stirling 38h kapalı, n8n 38h
host'tan erişilmez, sıfır alarm). Kritik davranış: docker-ps-running YETMEZ,
HTTP probe-fail = dead. Boot-grace FP'yi bastırır ama 5dk ile sınırlı.
"""

from __future__ import annotations

import subprocess
import urllib.error
from types import SimpleNamespace

import pytest

from app.core import liveness as lv


@pytest.fixture(autouse=True)
def _high_uptime(monkeypatch):
    """Boot-grace varsayılan-OFF (test_liveness.py ile aynı desen)."""
    monkeypatch.setattr(lv, "_uptime_s", lambda: 10**9)


def _mock_docker_ps(monkeypatch, names: list[str] | None, rc: int = 0, raise_exc=None):
    def fake_run(*args, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(returncode=rc, stdout="\n".join(names or []))

    monkeypatch.setattr(lv.subprocess, "run", fake_run)


class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_probe(monkeypatch, status: int | None = 200, raise_exc=None):
    def fake_urlopen(req, timeout=None):
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp(status)

    monkeypatch.setattr(lv.urllib.request, "urlopen", fake_urlopen)


def _by_source(results):
    return {r["source"]: r for r in results}


# ── boot-grace ──


def test_boot_grace_suppresses_during_docker_startup(monkeypatch):
    """Boot+<5dk: konteynerler meşru olarak henüz kalkıyor olabilir → tek
    'unknown', alarm yok (FP-önleme)."""
    monkeypatch.setattr(lv, "_uptime_s", lambda: 60.0)
    out = lv.docker_containers_liveness()
    assert len(out) == 1
    assert out[0]["status"] == "unknown"
    assert "boot-grace" in out[0]["detail"]


def test_boot_grace_expires_after_cap(monkeypatch):
    """Boot+>5dk: grace bitti, gerçek verdict (incident: sorun saatlerce sürdü —
    grace sonsuz olsaydı yine kör kalırdık)."""
    monkeypatch.setattr(lv, "_uptime_s", lambda: lv.DOCKER_BOOT_GRACE_S + 1)
    _mock_docker_ps(monkeypatch, [])
    out = lv.docker_containers_liveness()
    assert all(r["status"] == "dead" for r in out)


# ── docker ps katmanı ──


def test_docker_daemon_down_single_aggregate_dead(monkeypatch):
    """docker ps fail → 9 ayrı dead spam'i DEĞİL, tek toplu 'docker' dead."""
    _mock_docker_ps(monkeypatch, None, raise_exc=OSError("no docker"))
    out = lv.docker_containers_liveness()
    assert len(out) == 1
    assert out[0]["source"] == "docker"
    assert out[0]["status"] == "dead"


def test_docker_ps_nonzero_rc_dead(monkeypatch):
    _mock_docker_ps(monkeypatch, [], rc=1)
    out = lv.docker_containers_liveness()
    assert len(out) == 1
    assert out[0]["status"] == "dead"


def test_docker_ps_timeout_dead(monkeypatch):
    _mock_docker_ps(monkeypatch, None, raise_exc=subprocess.TimeoutExpired("docker", 10))
    out = lv.docker_containers_liveness()
    assert len(out) == 1
    assert out[0]["status"] == "dead"


def test_missing_container_dead(monkeypatch):
    """Beklenen konteyner docker ps'te yok (Exited/silinmiş) → dead."""
    running = [n for n in lv.DOCKER_CONTAINERS if n != "grafana"]
    _mock_docker_ps(monkeypatch, running)
    _mock_probe(monkeypatch, 200)
    by = _by_source(lv.docker_containers_liveness())
    assert by["docker:grafana"]["status"] == "dead"
    assert "çalışmıyor" in by["docker:grafana"]["detail"]
    assert by["docker:n8n"]["status"] == "alive"


# ── HTTP probe katmanı (incident imzası) ──


def test_running_but_probe_unreachable_dead(monkeypatch):
    """TAM INCIDENT İMZASI: docker 'running' diyor ama host'tan probe cevapsız
    (network/port-binding kopuk) → dead. docker-status'a güvenilseydi kör kalırdık."""
    _mock_docker_ps(monkeypatch, list(lv.DOCKER_CONTAINERS))
    _mock_probe(monkeypatch, raise_exc=urllib.error.URLError(ConnectionRefusedError()))
    out = lv.docker_containers_liveness()
    assert len(out) == len(lv.DOCKER_CONTAINERS)
    assert all(r["status"] == "dead" for r in out)
    assert all("probe-fail" in r["detail"] for r in out)


def test_running_probe_ok_alive(monkeypatch):
    _mock_docker_ps(monkeypatch, list(lv.DOCKER_CONTAINERS))
    _mock_probe(monkeypatch, 200)
    out = lv.docker_containers_liveness()
    assert len(out) == len(lv.DOCKER_CONTAINERS)
    assert all(r["status"] == "alive" for r in out)


def test_probe_http_5xx_dead(monkeypatch):
    _mock_docker_ps(monkeypatch, list(lv.DOCKER_CONTAINERS))
    _mock_probe(monkeypatch, raise_exc=urllib.error.HTTPError("u", 500, "ISE", {}, None))
    out = lv.docker_containers_liveness()
    assert all(r["status"] == "dead" for r in out)


def test_probe_timeout_dead(monkeypatch):
    _mock_docker_ps(monkeypatch, list(lv.DOCKER_CONTAINERS))
    _mock_probe(monkeypatch, raise_exc=TimeoutError())
    out = lv.docker_containers_liveness()
    assert all(r["status"] == "dead" for r in out)


# ── check_all entegrasyonu ──


def test_check_all_flattens_list_results(monkeypatch):
    """Liste dönen komponent düzleşir; her konteyner ayrı source = per-container
    edge-detection (yeni ölen her konteyner ayrı alarm)."""
    monkeypatch.setattr(
        lv,
        "REGISTRY",
        [
            lambda: {"source": "tek", "klass": "A", "status": "alive", "detail": ""},
            lambda: [
                {"source": "docker:a", "klass": "B", "status": "dead", "detail": ""},
                {"source": "docker:b", "klass": "B", "status": "alive", "detail": ""},
            ],
        ],
    )
    out = lv.check_all()
    assert [r["source"] for r in out["results"]] == ["tek", "docker:a", "docker:b"]
    assert [r["source"] for r in out["dead"]] == ["docker:a"]


def test_check_all_component_exception_isolated(monkeypatch):
    """Bir komponent patlasa diğerleri taranır (mevcut garanti listeyle bozulmadı)."""

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(
        lv,
        "REGISTRY",
        [boom, lambda: [{"source": "docker:a", "klass": "B", "status": "alive", "detail": ""}]],
    )
    out = lv.check_all()
    assert len(out["results"]) == 2
    assert out["results"][0]["status"] == "unknown"
