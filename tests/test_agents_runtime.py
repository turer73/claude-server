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


def test_codereview_card_signal_rate_14d_window():
    # Sinyal-oranı = GERÇEK (completed) ÷ TRİYAJ (completed+obsolete), SON-14-GÜN penceresinden.
    # Yaşam-boyu kümülatif oran qwen/FP-seli havuzunu (306 kayıt) paydada taşıyıp %16 gösteriyordu
    # (gerçek pipeline %88'ken); tüm-zaman artık stats'ta ayrı satır. 30g DEĞİL 14g: klipper verify
    # (PR#301) — 30g bugünün tarihinde kötü-havuzu hâlâ kapsıyordu, 14g bunu hemen dışlıyor.
    crdb = {
        "counts": {"active": 9, "completed": 15, "obsolete": 85},  # tüm-zaman: %15
        "counts_14d": {"completed": 8, "obsolete": 2},  # son-14g: %80 — ana metrik BU
        "findings": [
            {"time": "2026-06-20T09:00", "title": "app/api/dev.py:48 injection", "severity": "P1", "status": "active", "kind": "bug"}
        ],
    }
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["key"] == "code-review"
    assert card["success_rate"] == {"label": "Sinyal (gerçek÷triaj, 14g)", "value": 0.8, "n": 10}
    assert card["stats"]["Sinyal tüm-zaman"] == "15% (100)"
    assert card["current_task"] == "Son inceleme: app/api/dev.py:48"
    assert "claude-haiku-4-5-20251001 (tarama)" in card["models"]
    assert "claude-sonnet-4-6 (kontrol/sentez)" in card["models"]


def test_codereview_card_14d_empty_falls_back_lifetime():
    # 14g'de hiç triyaj yoksa (sessiz ay) tüm-zamana düş — ama etiket dürüst kalsın
    crdb = {"counts": {"completed": 2, "obsolete": 6}, "counts_14d": {}, "findings": []}
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["success_rate"] == {"label": "Sinyal (gerçek÷triaj, tüm-zaman)", "value": 0.25, "n": 8}
    assert "Sinyal tüm-zaman" not in card["stats"]  # zaten ana metrik tüm-zaman — çift gösterme


def test_codereview_card_missing_counts_14d_backcompat():
    # counts_14d anahtarı yoksa (eski üretici) counts'a düşer — kart çökmez
    crdb = {"counts": {"completed": 2, "obsolete": 6}, "findings": []}
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["success_rate"]["value"] == 0.25
    assert card["success_rate"]["n"] == 8


def test_codereview_card_empty():
    card = _codereview_card(_FakeCRA(), {"counts": {}, "counts_14d": {}, "findings": []})
    assert card["success_rate"] is None
    assert card["current_task"] == "Kuyruk/sweep bekliyor"


def test_codereview_db_14d_window(tmp_path, monkeypatch):
    # _codereview_db SQL'i: 14g penceresi yalnız yeni kayıtları saymalı (created_at TEXT 'YYYY-MM-DD HH:MM:SS')
    import sqlite3

    from app.api import agents

    db = tmp_path / "mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, status TEXT, title TEXT, details TEXT, created_at TEXT)"
    )
    rows = [
        # eski dönem (>14g): 1 completed + 3 obsolete
        ("completed", "-40 days"),
        ("obsolete", "-40 days"),
        ("obsolete", "-20 days"),
        ("obsolete", "-15 days"),
        # yeni dönem (<14g): 4 completed + 1 obsolete
        ("completed", "-5 days"),
        ("completed", "-4 days"),
        ("completed", "-2 days"),
        ("completed", "-1 days"),
        ("obsolete", "-3 days"),
    ]
    for status, delta in rows:
        con.execute(
            "INSERT INTO discoveries (project,type,status,title,details,created_at) "
            "VALUES ('code-review','bug',?,'t','[P2] x',datetime('now',?))",
            (status, delta),
        )
    con.commit()
    con.close()
    monkeypatch.setattr(agents, "MEMORY_DB", str(db))
    crdb = agents._codereview_db()
    assert crdb["counts"] == {"completed": 5, "obsolete": 4}
    assert crdb["counts_14d"] == {"completed": 4, "obsolete": 1}
    card = _codereview_card(_FakeCRA(), crdb)
    assert card["success_rate"]["value"] == 0.8  # 4/5 — 14g penceresi
    assert card["stats"]["Sinyal tüm-zaman"] == "56% (9)"  # 5/9 kümülatif, ayrı satırda
