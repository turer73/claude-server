"""claim-gate-poller — saf karar-mantığı testleri (GH/ağ yok; şablon: test_agent_health_report).

check_claim (repo kısa/tam-ad eşleşme, inactive/expired dışlama) + decide (advisory/enforce/
override karar-tablosu). Canlı GH-status POST'u test edilmez.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cgp", ROOT / "automation" / "claim-gate-poller.py")
cgp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cgp)


@pytest.fixture
def claims_db(tmp_path):
    db_path = tmp_path / "mem.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE active_claims (
            id INTEGER PRIMARY KEY, task_key TEXT, device TEXT, repo TEXT, branch TEXT,
            note TEXT DEFAULT '', active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')), expires_at TEXT, released_at TEXT
        );
        INSERT INTO active_claims (task_key, device, repo, branch, expires_at)
            VALUES ('claude-server:memory', 'surer', 'claude-server', 'feat/x', datetime('now','+2 hours'));
        INSERT INTO active_claims (task_key, device, repo, branch, active, expires_at)
            VALUES ('claude-server:eski', 'klipper', 'claude-server', 'feat/released', 0, datetime('now','+2 hours'));
        INSERT INTO active_claims (task_key, device, repo, branch, expires_at)
            VALUES ('claude-server:bayat', 'opencode', 'claude-server', 'feat/stale', datetime('now','-1 hours'));
    """)
    con.commit()
    return con


def test_check_claim_matches_short_and_full_repo(claims_db):
    # Ajanlar repo'yu kısa-ad yazıyor; bot GH'den tam-adla gelir — ikisi de eşleşmeli
    assert cgp.check_claim(claims_db, "turer73/claude-server", "feat/x")["device"] == "surer"
    assert cgp.check_claim(claims_db, "claude-server", "feat/x")["device"] == "surer"


def test_check_claim_excludes_released_and_expired(claims_db):
    assert cgp.check_claim(claims_db, "turer73/claude-server", "feat/released") is None  # active=0
    assert cgp.check_claim(claims_db, "turer73/claude-server", "feat/stale") is None  # TTL dolmuş
    assert cgp.check_claim(claims_db, "turer73/claude-server", "feat/hic-yok") is None


def test_decide_claim_present_success():
    state, desc = cgp.decide({"device": "surer", "task_key": "a:b", "expires_at": "t"}, enforce=False, has_override=False)
    assert state == "success"
    assert "surer" in desc


def test_decide_advisory_missing_no_status():
    # Advisory-modda claim-yok: status YAZILMAZ (yokken success=yanlış-güven, failure=fiili-blok)
    state, _ = cgp.decide(None, enforce=False, has_override=False)
    assert state is None


def test_decide_enforce_missing_failure():
    state, desc = cgp.decide(None, enforce=True, has_override=False)
    assert state == "failure"
    assert "CLAIM" in desc


def test_decide_override_label_escape_hatch():
    # Enforce-modda bile insan-onaylı kaçış-kapısı (watchdog-FP dersi) — AUDIT izli
    state, desc = cgp.decide(None, enforce=True, has_override=True)
    assert state == "success"
    assert "AUDIT" in desc


def test_check_claim_missing_table_returns_none(tmp_path):
    # Codex#304 bulgu-3: active_claims lazy-kurulur, ilk /claims öncesi hiç yok — crash değil None
    con = sqlite3.connect(tmp_path / "empty.db")
    con.row_factory = sqlite3.Row
    assert cgp.check_claim(con, "turer73/claude-server", "feat/x") is None


def test_check_claim_other_operational_error_reraises():
    # Sadece 'no such table' yutulur — başka DB-hatası (ör. kilit) sessizce gizlenmemeli
    class _LockedDB:
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        cgp.check_claim(_LockedDB(), "turer73/claude-server", "feat/x")


def test_should_post_clears_stale_success_when_claim_gone():
    # Codex#304 bulgu-1: claim kalkınca eski 'success' GH'de asılı kalmamalı — pending'e çekilir
    result = cgp._should_post(None, "advisory: claim yok", {"state": "success", "description": "eski"})
    assert result == (cgp.STALE_CLEAR_STATE, cgp.STALE_CLEAR_DESC)


def test_should_post_skips_when_no_claim_and_no_prior_status():
    # Advisory niyeti korunur: claim hiç olmadıysa (veya zaten pending) gürültü üretme
    assert cgp._should_post(None, "advisory: claim yok", None) is None
    assert cgp._should_post(None, "advisory: claim yok", {"state": "pending", "description": "x"}) is None


def test_should_post_dedups_identical_state():
    # Codex#304 bulgu-4: 1000-status/SHA limiti — aynı (state,desc) tekrar postalanmaz
    latest = {"state": "success", "description": "claim: surer (a:b, bitiş t)"}
    assert cgp._should_post("success", "claim: surer (a:b, bitiş t)", latest) is None


def test_should_post_posts_when_state_changed():
    latest = {"state": "success", "description": "claim: surer (a:b, bitiş eski)"}
    result = cgp._should_post("success", "claim: klipper (a:b, bitiş yeni)", latest)
    assert result == ("success", "claim: klipper (a:b, bitiş yeni)")


def test_next_page_url_parses_link_header():
    link = '<https://api.github.com/repos/x/y/pulls?page=2>; rel="next", <https://api.github.com/repos/x/y/pulls?page=5>; rel="last"'
    assert cgp._next_page_url(link) == "https://api.github.com/repos/x/y/pulls?page=2"


def test_next_page_url_none_on_last_page():
    link = '<https://api.github.com/repos/x/y/pulls?page=1>; rel="first"'
    assert cgp._next_page_url(link) is None
    assert cgp._next_page_url("") is None
