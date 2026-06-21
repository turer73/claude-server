"""automation/system-state.py — longitudinal sentez (LSA Faz-2) saf-fonksiyon testleri.

Canlı LLM/HTTP test edilmez; veri-toplama + render + write-payload şekli (skip_dedup, tarih-unique
başlık) test edilir. agent-health-report test deseni (importlib file-load)."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ss", ROOT / "automation" / "system-state.py")
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    srv = tmp_path / "server.db"
    con = sqlite3.connect(srv)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp TEXT, severity TEXT, source TEXT, title TEXT)")
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, timestamp TEXT, job TEXT, result TEXT)")
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, timestamp TEXT, severity TEXT, source TEXT, message TEXT, resolved INTEGER)")
    # kendi-iyileşen alarm (resolved) + tekrar-fail cron
    H = "datetime('now','-1 hours')"
    con.execute(f"INSERT INTO alerts (timestamp,severity,source,message,resolved) VALUES ({H},'critical','temperature','88C',1)")
    for _ in range(4):
        con.execute(f"INSERT INTO cron_outcomes (timestamp,job,result) VALUES ({H},'liveness-check','fail')")
    con.execute(f"INSERT INTO cron_outcomes (timestamp,job,result) VALUES ({H},'ok-job','pass')")
    con.execute(f"INSERT INTO events (timestamp,severity,source,title) VALUES ({H},'warn','code-review:x.py','🔬 P1 (commit): x')")
    con.commit()
    con.close()

    mem = tmp_path / "mem.db"
    con = sqlite3.connect(mem)
    con.execute("CREATE TABLE discoveries (id INTEGER PRIMARY KEY, created_at TEXT, type TEXT, title TEXT, status TEXT)")
    con.execute("INSERT INTO discoveries (created_at,type,title,status) VALUES (datetime('now','-1 hours'),'bug','eski açık bug','active')")
    con.commit()
    con.close()

    monkeypatch.setattr(ss, "SRV_DB", str(srv))
    monkeypatch.setattr(ss, "MEM_DB", str(mem))
    return srv, mem


def test_gather_state_longitudinal(dbs):
    st = ss.gather_state(7)
    assert st["cron_result"].get("fail") == 4
    assert any(j == "liveness-check" and c == 4 for j, c in st["cron_recurring_fail"])  # tekrar-fail trend
    assert any(src == "temperature" and r == 1 for src, c, r in st["alerts_fired"])  # kendi-iyileşen (resolved=1)
    assert st["code_review_findings"][0][0] == 1  # commit-bulgu sayıldı


def test_render_data_no_crash_empty(monkeypatch, tmp_path):
    # Eksik/boş DB → fail-safe (sqlite_error → boş liste), render patlamaz
    monkeypatch.setattr(ss, "SRV_DB", str(tmp_path / "yok.db"))
    monkeypatch.setattr(ss, "MEM_DB", str(tmp_path / "yok2.db"))
    txt = ss.render_data(ss.gather_state(7))
    assert "SİSTEM DURUMU HAM VERİ" in txt


def test_write_state_payload_skip_dedup(dbs, monkeypatch):
    captured = {}

    def fake_post(url, body, headers, timeout):
        captured["url"] = url
        captured["body"] = body
        return {}

    monkeypatch.setattr(ss, "_post_json", fake_post)
    err = ss.write_state("ham", "anlatı", "mkey")
    assert err == ""
    assert captured["body"]["skip_dedup"] is True  # günlük-log dedup'tan korunur
    assert captured["body"]["type"] == "learning"
    assert captured["body"]["title"].startswith("Sistem Durumu — ")  # tarih-unique
