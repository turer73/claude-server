"""scripts/seo-ctr-watch.py — saf izleme mantığı (GSC/gh/ağ yok, mock).

due_checkpoints (vade) + verdict (CTR/pos yorumu) + state I/O round-trip test edilir.
Auth/GSC/gh test edilmez (canlı bağımlılık).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("seo_ctr_watch", ROOT / "scripts" / "seo-ctr-watch.py")
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)


# ── due_checkpoints ──────────────────────────────────────────────────────────


def test_due_none_before_first_week():
    assert w.due_checkpoints(3, []) == []


def test_due_week1_at_7_days():
    assert w.due_checkpoints(7, []) == ["week1"]


def test_due_skips_already_reported():
    assert w.due_checkpoints(14, ["week1"]) == ["week2"]


def test_due_catches_up_multiple_if_gap():
    # 28 günde hiç raporlanmadıysa üçü birden vadeli
    assert w.due_checkpoints(28, []) == ["week1", "week2", "week4"]


def test_due_all_reported_empty():
    assert w.due_checkpoints(40, ["week1", "week2", "week4"]) == []


# ── verdict ──────────────────────────────────────────────────────────────────


def test_verdict_success_above_target():
    v = w.verdict(3.5, 4.4, is_final=False)
    assert "BAŞARILI" in v


def test_verdict_improvement():
    v = w.verdict(2.8, 4.4, is_final=False)
    assert "İYİLEŞME" in v


def test_verdict_regression():
    v = w.verdict(1.0, 4.4, is_final=False)
    assert "GERİLEME" in v


def test_verdict_no_change_final_suggests_revert():
    v = w.verdict(1.95, 4.42, is_final=True)
    assert "DEĞİŞİM YOK" in v
    assert "revize" in v


def test_verdict_position_improvement_noted():
    v = w.verdict(2.0, 3.5, is_final=False)
    assert "pozisyon iyileşti" in v


# ── state round-trip ─────────────────────────────────────────────────────────


def test_state_roundtrip(tmp_path, monkeypatch):
    sf = tmp_path / "state.json"
    monkeypatch.setattr(w, "STATE_FILE", str(sf))
    assert w.load_state() == {"merged_at": None, "reported": [], "concluded": False}
    w.save_state({"merged_at": "2026-06-20", "reported": ["merge"], "concluded": False})
    assert json.loads(sf.read_text())["merged_at"] == "2026-06-20"
    assert w.load_state()["reported"] == ["merge"]
