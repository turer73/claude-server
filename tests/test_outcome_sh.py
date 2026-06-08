"""scripts/lib/outcome.sh — OUTCOME-contract helper'ları (bash, subprocess ile test)."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "lib" / "outcome.sh"


def _sh(snippet: str) -> str:
    r = subprocess.run(["bash", "-c", f". {LIB}\n{snippet}"], capture_output=True, text=True)
    return r.stdout.strip()


def test_emit_outcome_valid():
    assert _sh('emit_outcome pass "tamam"') == "OUTCOME: pass | tamam"


def test_emit_outcome_invalid_forces_fail():
    # geçersiz result → fail (sessiz-yeşil önle)
    assert _sh('emit_outcome bogus "x"') == "OUTCOME: fail | x"


def test_numeric_floor_none_executed_is_fail():
    assert _sh("numeric_floor 0 5") == "fail"


def test_numeric_floor_partial():
    assert _sh("numeric_floor 3 5") == "partial"


def test_numeric_floor_all_executed_is_pass():
    assert _sh("numeric_floor 5 5") == "pass"


def test_numeric_floor_bad_total_is_fail():
    # Codex P2: total geçersiz/eksik + executed>0 → pass DEME, fail
    assert _sh('numeric_floor 3 ""') == "fail"
    assert _sh("numeric_floor 2 0") == "fail"


def test_json_floor(tmp_path):
    ok = tmp_path / "ok.json"
    ok.write_text('{"a":1}')
    bad = tmp_path / "bad.json"
    bad.write_text("not json {")
    empty = tmp_path / "e.json"
    empty.write_text("")
    assert _sh(f"json_floor {ok} && echo OK || echo NO") == "OK"
    assert _sh(f"json_floor {bad} && echo OK || echo NO") == "NO"
    assert _sh(f"json_floor {empty} && echo OK || echo NO") == "NO"
    assert _sh(f"json_floor {tmp_path}/yok.json && echo OK || echo NO") == "NO"


def test_floor_from_status(tmp_path):
    # executed-floor: status dosyası domain\t<0|1> → numeric_floor
    allf = tmp_path / "all.tsv"
    allf.write_text("a\t1\nb\t1\n")
    mix = tmp_path / "mix.tsv"
    mix.write_text("a\t1\nb\t0\n")
    blk = tmp_path / "blk.tsv"
    blk.write_text("a\t0\nb\t0\n")
    assert _sh(f"floor_from_status {allf}") == "pass"
    assert _sh(f"floor_from_status {mix}") == "partial"
    assert _sh(f"floor_from_status {blk}") == "fail"
    # eksik/boş dosya → fail (hiç iş kaydı yok = güvenli-fail)
    assert _sh(f"floor_from_status {tmp_path}/yok.tsv") == "fail"
