"""Ajan-runtime dashboard endpoint testleri — last-run/iş/bulgu/model/başarı kartları."""

from app.api.agents import _codereview_card, _devops_card, _sev_from_details


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
            "model": "qwen2.5-coder:7b",
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
    assert "qwen2.5-coder:7b (review)" in card["models"]


def test_codereview_card_empty():
    card = _codereview_card(_FakeCRA(), {"counts": {}, "findings": []})
    assert card["success_rate"] is None
    assert card["current_task"] == "Kuyruk/sweep bekliyor"
