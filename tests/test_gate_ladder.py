"""G6 gate-ladder testleri (PR-G6a): eval-çekirdeği doğrudan (python) + wrapper (bash-shim).

Python-çekirdek app-import çekmez (saf-stdlib) → doğrudan import+çağrı, heredoc-extract yok.
G1-repro: base'de gate_ladder_eval.py yok → import-hatası FAIL. Anlamlı-mekanik kanıt
testlerin kendisi: production-dedup + 4-kriter + recommend-only (rung-değişmez).
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from automation.gate_ladder_eval import (
    MIN_FIRINGS,
    GateStats,
    evaluate,
    production_stats,
    run_eval,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _db(tmp_path) -> Path:
    db = tmp_path / "coverage.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE gate_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT, gate_id TEXT, run_id INTEGER, pr_number INTEGER,
            head_sha TEXT, branch TEXT, ts TEXT, verdict TEXT,
            fp_class TEXT DEFAULT 'unknown', fp_source TEXT, note TEXT, UNIQUE(gate_id, run_id));
        CREATE TABLE gate_ladder (
            gate_id TEXT PRIMARY KEY, rung TEXT DEFAULT 'non_required',
            since_ts TEXT DEFAULT (datetime('now')), last_eval TEXT, history_json TEXT DEFAULT '[]');
        """
    )
    con.commit()
    con.close()
    return db


def _seed(db, gate, run_id, pr, verdict, fp_class="unknown", fp_source=None):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO gate_telemetry (gate_id, run_id, pr_number, ts, verdict, fp_class, fp_source) VALUES (?,?,?,datetime('now'),?,?,?)",
        (gate, run_id, pr, verdict, fp_class, fp_source),
    )
    con.commit()
    con.close()


# ── production-dedup (spec-verify #100406) ──────────────────────────────────────


def test_production_dedup_same_pr_counts_once(tmp_path):
    """Aynı-PR'ın 3 push'u (dev-iterasyon) 1-firing sayılır — SON run'ın verdict'i geçerli."""
    db = _db(tmp_path)
    _seed(db, "g1-repro", 101, 5, "fail")  # ilk-push fail
    _seed(db, "g1-repro", 102, 5, "fail")  # 2.push hâlâ fail
    _seed(db, "g1-repro", 103, 5, "pass")  # 3.push düzeltti (SON)
    con = sqlite3.connect(db)
    stats = production_stats(con)
    con.close()
    # SON run pass → fail-firing 0 (dev-iterasyonu tekilleşti, tekrar-sayım yok)
    assert stats["g1-repro"].firing == 0


def test_precision_and_human_fraction(tmp_path):
    db = _db(tmp_path)
    # 4 ayrı-PR fail: 3 true_catch + 1 false_positive (hepsi human) → precision 0.75
    for i, (rid, pr, cls) in enumerate([(1, 1, "true_catch"), (2, 2, "true_catch"), (3, 3, "true_catch"), (4, 4, "false_positive")]):
        _seed(db, "g4-invariant", rid, pr, "fail", cls, "human")
    con = sqlite3.connect(db)
    s = production_stats(con)["g4-invariant"]
    con.close()
    assert s.firing == 4
    assert s.precision == 0.75
    assert s.human_fraction == 1.0


# ── evaluate() 4-kriter (tasarım §3) ────────────────────────────────────────────


def test_evaluate_promote_all_criteria_met():
    s = GateStats("g1-repro", firing=25, tc_human=24, fp_human=1, unclassified=0)  # precision≈0.96
    assert evaluate("non_required", s).action == "promote"


def test_evaluate_hold_thin_data():
    s = GateStats("g1-repro", firing=5, tc_human=5, fp_human=0, unclassified=0)  # precision 1.0 ama firing<20
    r = evaluate("non_required", s)
    assert r.action == "hold"
    assert "firing" in r.reason


def test_evaluate_hold_no_ground_truth():
    s = GateStats("g1-repro", firing=30, tc_human=0, fp_human=0, unclassified=30)  # hepsi-unknown
    r = evaluate("non_required", s)
    assert r.action == "hold"
    assert "ground-truth" in r.reason  # fail-safe: veri-yok → terfi-yok


def test_evaluate_hold_low_precision():
    s = GateStats("g1-repro", firing=25, tc_human=15, fp_human=10, unclassified=0)  # precision 0.6
    assert evaluate("non_required", s).action == "hold"


def test_evaluate_demote_required_drift():
    s = GateStats("g4-invariant", firing=25, tc_human=12, fp_human=10, unclassified=3)  # precision≈0.55
    assert evaluate("required", s).action == "demote"


def test_evaluate_shadow_off_no_auto():
    s = GateStats("x", firing=30, tc_human=30, fp_human=0, unclassified=0)
    assert evaluate("shadow", s).action == "hold"
    assert evaluate("off", s).action == "hold"


# ── run_eval recommend-only (rung DEĞİŞMEZ) ─────────────────────────────────────


def test_run_eval_does_not_change_rung(tmp_path):
    """G6 ÇEKİRDEK-GARANTİ (§4): eval basamak-DEĞİŞTİRMEZ, yalnız history'e öneri-ekler.
    Terfi-uygun gate bile non_required KALIR (aktüasyon Turgut'ta)."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO gate_ladder (gate_id, rung) VALUES ('g1-repro', 'non_required')")
    con.commit()
    for rid in range(1, 26):  # 25 ayrı-PR, hepsi true_catch → terfi-uygun
        con.execute(
            "INSERT INTO gate_telemetry (gate_id, run_id, pr_number, ts, verdict, fp_class, fp_source) "
            "VALUES ('g1-repro', ?, ?, datetime('now'), 'fail', 'true_catch', 'human')",
            (rid, rid),
        )
    con.commit()
    recs = run_eval(con, days=30)
    assert [r.action for r in recs] == ["promote"]  # öneri promote
    rung = con.execute("SELECT rung FROM gate_ladder WHERE gate_id='g1-repro'").fetchone()[0]
    con.close()
    assert rung == "non_required"  # AMA rung DEĞİŞMEDİ — recommend-only


# ── bash-wrapper uçtan-uca (CI-otoriter) ────────────────────────────────────────

_MISSING = [t for t in ("bash", "sqlite3", "python3") if shutil.which(t) is None]


@pytest.mark.skipif(bool(_MISSING), reason=f"CI-otoriter; lokalde eksik: {_MISSING}")
def test_wrapper_end_to_end_no_promotion(tmp_path):
    """Gerçek-yol: migration + eval-wrapper; thin-data → hold, note-atmaz (KEY-yok fail-safe)."""
    db = tmp_path / "coverage.db"
    env = {
        "COVERAGE_DB": str(db),
        "GATE_LADDER_LOG": str(tmp_path / "g6.log"),
        "HOOK_ENV_FILE": str(tmp_path / "yok.env"),  # KEY-yok → note-skip
    }
    import os

    log_path = tmp_path / "g6.log"
    r = subprocess.run(
        ["bash", str(REPO_ROOT / "automation" / "gate-ladder-eval.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=60,
    )
    logtail = log_path.read_text(encoding="utf-8")[-800:] if log_path.exists() else "(log yok)"
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr[-400:]} LOG={logtail}"
    assert "hold" in r.stdout  # 2-seed-gate thin-data → hold
    assert MIN_FIRINGS  # sanity: modül-sabiti import-edildi
