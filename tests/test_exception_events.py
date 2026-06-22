"""Tests for app/middleware/exception_events.py (gap-2 exception-producer).

Klipper Linux-verify kriterleri burada birim+entegrasyon olarak kapsanır:
404/422 → emit ETMEZ, unhandled-5xx → emit EDER, dedup, KVKK-redact, novelty.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.middleware import exception_events as ee


def _events_db(tmp_path):
    p = tmp_path / "server.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, "
        "severity TEXT DEFAULT 'info', title TEXT, detail TEXT, payload TEXT, "
        "notified INTEGER DEFAULT 0)"
    )
    con.commit()
    con.close()
    return str(p)


def _rows(db, type_="exception"):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM events WHERE type=?", (type_,)).fetchall()
    con.close()
    return rows


# ---- saf birim: frame-detection + fingerprint ----


def test_is_app_frame_real_paths():
    assert ee._is_app_frame(os.path.join(ee._APP_DIR, "api", "shell.py")) is True
    assert ee._is_app_frame("/usr/lib/python3.14/site-packages/starlette/routing.py") is False
    assert ee._is_app_frame("<frozen importlib._bootstrap>") is False
    # tests/ app/ altında değil → app-frame değil
    assert ee._is_app_frame(os.path.join(ee._APP_ROOT, "tests", "x.py")) is False


def test_rel_module():
    p = os.path.join(ee._APP_ROOT, "app", "api", "shell.py")
    assert ee._rel_module(p) == "app/api/shell.py"


def test_fingerprint_excludes_line_and_is_stable():
    here = lambda fn: fn.endswith("test_exception_events.py")  # noqa: E731

    def _boom():
        raise ValueError("x")

    fps = []
    for _ in range(2):
        try:
            _boom()
        except ValueError as e:
            fps.append(ee.fingerprint(e, is_app=here))
    assert fps[0] == fps[1]  # line dahil değil → 2 çağrı aynı
    assert fps[0].startswith("ValueError:")
    assert fps[0].endswith(":_boom")
    assert not any(part.isdigit() for part in fps[0].split(":"))  # line-no yok


def test_fingerprint_no_app_frame_sentinel():
    try:
        raise RuntimeError("y")
    except RuntimeError as e:
        fp = ee.fingerprint(e, is_app=lambda fn: False)  # hiçbir frame app değil
    assert fp == "RuntimeError:<no-app-frame>"


# ---- gate ----


def test_disabled_gate_returns_none(monkeypatch):
    monkeypatch.setenv("EXCEPTION_EVENTS_ENABLED", "0")
    assert ee.record_exception_event(ValueError("x"), method="GET", path="/x") is None


# ---- route_template (KVKK: PII-maskeli template) ----


def test_route_template_prefers_template():
    route = SimpleNamespace(path="/api/v1/items/{id}")
    req = SimpleNamespace(scope={"route": route}, url=SimpleNamespace(path="/api/v1/items/5"))
    assert ee.route_template(req) == "/api/v1/items/{id}"


def test_route_template_fallback_to_path():
    req = SimpleNamespace(scope={}, url=SimpleNamespace(path="/raw/path"))
    assert ee.route_template(req) == "/raw/path"


# ---- entegrasyon: gerçek FastAPI exception-routing ----


def _make_app():
    app = FastAPI()

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):  # main.py'daki wiring'in aynısı
        method = request.method
        path = ee.route_template(request)
        await asyncio.to_thread(ee.record_exception_event, exc, method=method, path=path)
        return JSONResponse(status_code=500, content={"error": "InternalError"})

    @app.get("/boom")
    async def boom():
        raise ValueError("secret-user@example.com sizinti")

    @app.get("/notfound")
    async def notfound():
        raise HTTPException(status_code=404, detail="nope")

    @app.get("/item/{n}")
    async def item(n: int):
        return {"n": n}

    return app


def test_unhandled_5xx_emits_4xx_does_not(monkeypatch, tmp_path):
    db = _events_db(tmp_path)
    monkeypatch.setenv("DB_PATH", db)
    monkeypatch.delenv("EXCEPTION_EVENTS_ENABLED", raising=False)  # default ON
    # test-frame'leri app-frame say (gerçek extraction'ı egzersiz et)
    monkeypatch.setattr(ee, "_APP_DIR", os.path.join(ee._APP_ROOT, "tests"))
    client = TestClient(_make_app(), raise_server_exceptions=False)

    # 1) unhandled 5xx → emit EDER
    assert client.get("/boom").status_code == 500
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "warn"  # klipper #100136: warn-ile-başla (critical değil)
    assert row["source"].startswith("exception:ValueError:")
    assert '"novel": true' in row["payload"]
    assert '"method": "GET"' in row["payload"]
    assert '"path": "/boom"' in row["payload"]  # route-template

    # 2) KVKK: ham exc-mesajı HİÇBİR alanda yok
    blob = f"{row['title']} {row['detail']} {row['payload']}"
    assert "secret-user@example.com" not in blob

    # 3) 404 (HTTPException) → emit ETMEZ
    assert client.get("/notfound").status_code == 404
    assert len(_rows(db)) == 1  # yeni event yok

    # 4) 422 (validation) → emit ETMEZ
    assert client.get("/item/abc").status_code == 422
    assert len(_rows(db)) == 1

    # 5) aynı exception tekrar → dedup (pencere-içi suppress)
    assert client.get("/boom").status_code == 500
    assert len(_rows(db)) == 1  # hâlâ 1 (re-emit yok)
