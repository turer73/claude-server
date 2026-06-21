"""Ajan-runtime dashboard endpoint testleri — last-run/iş/bulgu/model/başarı kartları."""

from app.api.agents import (
    _AGENT_MANIFEST,
    _CRON_SCRIPTS,
    _codereview_card,
    _cron_card,
    _devops_card,
    _research_card,
    _sev_from_details,
)


def test_manifest_covers_decision_agents():
    keys = {a["key"] for a in _AGENT_MANIFEST}
    # SEO/ads/data-analiz/research/memory ajanları manifeste dahil mi
    for k in ("research", "ad-advisor", "data-analyst", "seo-gsc", "memory-triage", "autonomous-daily-summary"):
        assert k in keys, f"{k} manifeste eksik"
    # cron ajanları allowlist'li script'e sahip (manuel-tetikleme güvenliği)
    for a in _AGENT_MANIFEST:
        if a["type"] == "cron":
            assert a["key"] in _CRON_SCRIPTS


def test_cron_card_no_log_no_events():
    spec = {"key": "x", "name": "X", "role": "r", "schedule": "günlük", "models": ["m"], "log": None, "evsrc": None}
    card = _cron_card(spec)
    assert card["type"] == "cron"
    assert card["last_run"] is None  # log+event yok → dürüst None (uydurma yok)
    assert card["running"] is False
    assert card["triggerable"] is True


def test_cron_card_success_rate_from_outcomes(tmp_path, monkeypatch):
    """cron-kart success_rate + son-koşu cron_outcomes'tan gelir (hardcoded None DEĞİL — 'süs' algısı fix)."""
    import sqlite3

    from app.api import agents

    db = tmp_path / "srv.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    for r in ("pass", "pass", "fail", "pass"):
        con.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('data-analyst',?,datetime('now'))", (r,))
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "server_db_path", lambda: str(db))
    spec = {"key": "data-analyst", "name": "Veri", "role": "r", "schedule": "haftalık", "models": ["m"], "log": None, "evsrc": None}
    card = agents._cron_card(spec)
    assert card["success_rate"]["value"] == 0.75  # 3/4 pass — GERÇEK oran (henüz-veri-yok DEĞİL)
    assert card["success_rate"]["n"] == 4
    assert card["last_run"] is not None  # cron_outcomes'tan son-koşu
    assert card["running"] is True


def test_research_card_ondemand_not_triggerable():
    spec = {"key": "research", "name": "Araştırma", "role": "r", "schedule": "istek-üzerine", "models": ["qwen", "claude CLI"]}
    card = _research_card(
        spec, {"findings": [{"time": "t", "title": "[araştırma] FastAPI", "severity": "", "status": "active", "kind": "research"}], "n": 1}
    )
    assert card["type"] == "ondemand"
    assert card["triggerable"] is False  # topic gerekir → API'den
    assert "FastAPI" in card["current_task"]


def test_sev_from_details():
    assert _sev_from_details("[P1] injection") == "P1"
    assert _sev_from_details("[P2] x") == "P2"
    assert _sev_from_details("açıklama, sev yok") == ""


class _FakeRemediation:
    def __init__(self, source, action, success):
        self.timestamp = "2026-06-20T10:00:00"
        self.alert_source = source
        self.action = action
        self.success = success


class _FakeDevOps:
    _diag_model = "qwen2.5:3b"

    def __init__(self, log):
        self._remediation_log = log

    @property
    def status(self):
        return {"running": True, "last_check": "2026-06-20T10:05:00", "check_count": 42, "active_alerts": 1, "interval_seconds": 30}


def test_devops_card_success_rate_and_findings():
    log = [_FakeRemediation("service:x", "restart", True), _FakeRemediation("docker:y", "restart", False)]
    card = _devops_card(_FakeDevOps(log))
    assert card["key"] == "devops"
    assert card["running"] is True
    assert card["models"] == ["qwen2.5:3b (teşhis)"]
    assert card["success_rate"] == {"label": "Remediation başarısı", "value": 0.5, "n": 2}
    assert card["current_task"].startswith("Remediation: 1")  # aktif uyarı var
    assert len(card["findings"]) == 2
    assert card["findings"][0]["severity"] in ("P1", "P3")


def test_devops_card_no_remediation_no_rate():
    card = _devops_card(_FakeDevOps([]))
    assert card["success_rate"] is None
    assert card["current_task"].startswith("Remediation: 1")  # aktif uyarı (status'ta 1)


class _FakeCRA:
    def status(self):
        return {
            "enabled": True,
            "model": "claude-haiku-4-5-20251001",  # tarama route (LLM_ROUTE_CODE_REVIEW)
            "verify_model": "claude-sonnet-4-6",  # kontrol/sentez route (LLM_ROUTE_VERIFY)
            "synthesis_model": "claude-sonnet-4-6",
            "interval_s": 300,
            "ticks": 7,
            "total_findings": 9,
            "last_run": "2026-06-20T09:00:00",
        }


def test_codereview_card_signal_rate():
    crdb = {
        "counts": {"active": 9, "obsolete": 3},
        "findings": [
            {"time": "2026-06-20T09:00", "title": "app/api/dev.py:48 injection", "severity": "P1", "status": "active", "kind": "bug"}
        ],
    }
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["key"] == "code-review"
    assert card["success_rate"] == {"label": "Sinyal (FP-değil)", "value": 0.75, "n": 12}  # 9/(9+3)
    assert card["current_task"] == "Son inceleme: app/api/dev.py:48"
    assert "claude-haiku-4-5-20251001 (tarama)" in card["models"]
    assert "claude-sonnet-4-6 (kontrol/sentez)" in card["models"]


def test_codereview_card_empty():
    card = _codereview_card(_FakeCRA(), {"counts": {}, "findings": []})
    assert card["success_rate"] is None
    assert card["current_task"] == "Kuyruk/sweep bekliyor"
