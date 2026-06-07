"""LIVESYS-SENSE PR2 — self-pentest + nuclei executed-floor entegrasyonu (wiring).

Kör-tarama (WAF-blok / docker-fail) ≠ temiz. Script'ler outcome.sh source eder,
domain başına exec-status (1=tarandı/0=taranamadı) yazar, sonda floor_from_status ile
OUTCOME emit eder. floor_from_status davranışı test_outcome_sh.py'de; burada wiring.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF_PENTEST = (ROOT / "automation" / "self-pentest.sh").read_text()
NUCLEI = (ROOT / "automation" / "nuclei-scan.sh").read_text()


def test_self_pentest_sources_outcome_and_emits():
    assert "scripts/lib/outcome.sh" in SELF_PENTEST  # helper source
    assert "exec-status.tsv" in SELF_PENTEST  # executed tracking
    assert "floor_from_status" in SELF_PENTEST  # sonda floor
    assert "emit_outcome" in SELF_PENTEST  # sonda OUTCOME


def test_nuclei_sources_outcome_and_emits():
    assert "scripts/lib/outcome.sh" in NUCLEI
    assert "exec-status.tsv" in NUCLEI
    assert "floor_from_status" in NUCLEI
    assert "emit_outcome" in NUCLEI
    assert "drc=$?" in NUCLEI  # docker rc yakalanıyor (executed kararı için)


def test_both_removed_from_allowlist():
    allow = (ROOT / "tools" / "cron-outcome-allowlist.txt").read_text()
    active = [ln.strip() for ln in allow.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert "self-pentest.sh" not in active  # artık emit ediyor
    assert "nuclei-scan.sh" not in active
