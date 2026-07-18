"""telegram_poller.py startup-resilience testleri (ağ yok, mock)."""

from __future__ import annotations

import importlib.util
import os

import requests

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "telegram_poller.py")
_spec = importlib.util.spec_from_file_location("telegram_poller", _P)
tp = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(tp)  # type: ignore[union-attr]


class _Resp:
    def __init__(self, data: dict, ok: bool = True):
        self._data = data
        self.ok = ok

    def json(self) -> dict:
        return self._data


def test_startup_gate_retries_until_network_ready(monkeypatch):
    # Boot-DNS-lag simülasyonu: ilk 2 getMe ConnectionError, 3.'de ok →
    # retry (ağ toparlayana dek) + ardından webhook temizle. Eski kod tek-atıştı → fail'de
    # webhook temizlenmez (kalıcı 409 riski) + ilk getUpdates tam-traceback basardı.
    calls = {"getMe": 0, "deleteWebhook": 0}

    def fake_get(url, timeout=0):
        assert "getMe" in url
        calls["getMe"] += 1
        if calls["getMe"] < 3:
            raise requests.exceptions.ConnectionError("Temporary failure in name resolution")
        return _Resp({"ok": True, "result": {"username": "bot", "id": 1}})

    def fake_post(url, json=None, timeout=0):
        assert "deleteWebhook" in url
        calls["deleteWebhook"] += 1
        return _Resp({"ok": True}, ok=True)

    monkeypatch.setattr(tp.requests, "get", fake_get)
    monkeypatch.setattr(tp.requests, "post", fake_post)
    monkeypatch.setattr(tp.time, "sleep", lambda s: None)  # backoff'u atla
    tp._startup_gate()
    assert calls["getMe"] == 3  # 2 fail + 1 success (retry çalıştı)
    assert calls["deleteWebhook"] == 1  # ağ hazır olunca webhook temizlendi


def test_startup_gate_no_infinite_loop_on_api_error(monkeypatch):
    # Token/API hatası (ok=False ama YANIT var) → ağ hazır demektir; retry ETME, webhook'a geç.
    # (Aksi halde geçersiz-token sonsuz döngü yapardı.)
    calls = {"getMe": 0, "deleteWebhook": 0}

    def fake_get(url, timeout=0):
        calls["getMe"] += 1
        return _Resp({"ok": False, "error_code": 401, "description": "Unauthorized"})

    def fake_post(url, json=None, timeout=0):
        calls["deleteWebhook"] += 1
        return _Resp({"ok": True}, ok=True)

    monkeypatch.setattr(tp.requests, "get", fake_get)
    monkeypatch.setattr(tp.requests, "post", fake_post)
    monkeypatch.setattr(tp.time, "sleep", lambda s: None)
    tp._startup_gate()
    assert calls["getMe"] == 1  # tek yanıt → retry yok (sonsuz-döngü yok)
    assert calls["deleteWebhook"] == 1
