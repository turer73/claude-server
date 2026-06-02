"""Tests for LIVESYS PR-review FAZ1 in app/core/digest.py — aggregate→digest.
Pure observer (gh okuma); _gh_json monkeypatch'lenir (CI'da gerçek-network yok)."""

from __future__ import annotations

from app.core import digest as core_digest


# ── _pr_ci_state (pure) ──


def test_ci_state_green_failing_pending_unknown():
    assert core_digest._pr_ci_state([{"conclusion": "SUCCESS"}]) == "green"
    assert core_digest._pr_ci_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"
    assert core_digest._pr_ci_state([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]) == "pending"
    assert core_digest._pr_ci_state([{"conclusion": "SUCCESS"}, {"state": "PENDING"}]) == "pending"  # legacy
    assert core_digest._pr_ci_state([]) == "unknown"


# ── pr_review_health (gh mock) ──


def _fake_gh(pr_map, codex_map=None, fail_repos=()):
    codex_map = codex_map or {}

    def _gh(args, timeout=8.0):
        if args[0] == "pr":  # ["pr","list","-R",repo,...]
            repo = args[3]
            if repo in fail_repos:
                return None  # fetch-fail
            return pr_map.get(repo, [])
        if args[0] == "api":  # ["api","repos/O/R/pulls/N/comments","--jq",...]
            return codex_map.get(args[1], 0)
        return None

    return _gh


def test_pr_review_aggregates_open_prs(monkeypatch):
    pr_map = {
        "turer73/claude-server": [
            {"number": 20, "title": "feat x", "isDraft": False, "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
            {"number": 21, "title": "wip", "isDraft": True, "statusCheckRollup": []},  # draft → atla
        ],
    }
    codex_map = {"repos/turer73/claude-server/pulls/20/comments": 2}
    monkeypatch.setattr(core_digest, "REVIEW_REPOS", ["turer73/claude-server"])
    monkeypatch.setattr(core_digest, "_gh_json", _fake_gh(pr_map, codex_map))
    out = core_digest.pr_review_health()
    assert len(out["prs"]) == 1  # draft hariç
    p = out["prs"][0]
    assert p["repo"] == "claude-server"
    assert p["num"] == 20
    assert p["ci"] == "green"
    assert p["codex"] == 2
    assert out["fetch_fail"] is False


def test_pr_review_fetch_fail_not_silent(monkeypatch):
    # gh hata → fetch_fail=True (sessiz-sıfır DEĞİL; Codex-P1 dersi)
    monkeypatch.setattr(core_digest, "REVIEW_REPOS", ["turer73/panola"])
    monkeypatch.setattr(core_digest, "_gh_json", _fake_gh({}, fail_repos=("turer73/panola",)))
    out = core_digest.pr_review_health()
    assert out["fetch_fail"] is True
    assert out["prs"] == []


# ── has_signal ──


def _base():
    return {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "system": {"service": "active"},
        "vps": {},
        "cron_jobs": {},
        "liveness": {},
        "ci": {},
    }


def test_has_signal_open_pr_triggers():
    base = _base()
    base["pr_review"] = {
        "prs": [{"repo": "x", "num": 1, "ci": "green", "codex": 0, "title": "t"}],
        "signaled": [{"num": 1}],
        "fetch_fail": False,
    }
    assert core_digest.has_signal(base) is True


def test_has_signal_fetch_fail_triggers():
    base = _base()
    base["pr_review"] = {"prs": [], "signaled": [], "fetch_fail": True}
    assert core_digest.has_signal(base) is True


def test_has_signal_no_open_pr_no_signal():
    base = _base()
    base["pr_review"] = {"prs": [], "signaled": [], "fetch_fail": False}
    assert core_digest.has_signal(base) is False


# ── render ──


def test_render_includes_pr_review():
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "pr_review": {
            "prs": [{"repo": "panola", "num": 7, "ci": "green", "codex": 1, "title": "fix auth"}],
            "signaled": [{"num": 7}],
            "fetch_fail": False,
        },
        "system": {"service": "active", "disk_used_pct": "10%", "disk_avail": "9G", "mem_used_mb": "100", "mem_total_mb": "8000"},
    }
    text = core_digest.render_text(d)
    html = core_digest.render_html(d)
    assert "panola#7" in text
    assert "codex:1" in text
    assert "panola#7" in html
