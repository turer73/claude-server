"""LIVESYS FAZ 4 S1 — blast-radius.sh dogfood testi.

Kendi-üstümüzde doğrula: events.py'nin etki-haritası deterministik + temiz olmalı
(tablo=events, consumers FAZ3.2 wiring; Python-import/prose false-positive YOK).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "blast-radius.sh"


def _run(*args):
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def test_events_py_blast_radius_clean():
    r = _run("app/core/events.py")
    assert r.returncode == 0
    out = r.stdout
    # forward: SADECE events tablosu (2-hop database.py şema-home atlanır)
    assert "- events" in out
    # FAZ3.2 wiring consumer'ları yüzeye çıkmalı
    assert "scripts/emit-event.sh" in out
    assert "scripts/klipper-cron-wrap.sh" in out
    assert "app/core/devops_agent.py" in out  # emit_event import eder
    # false-positive OLMAMALI (Python import / prose "from")
    for fp in ("__future__", "- app\n", "- os\n", "- json\n", "- sqlite3"):
        assert fp not in out, f"false-positive tablo: {fp!r}"


def test_missing_file_exits_nonzero():
    r = _run("app/core/does_not_exist_xyz.py")
    assert r.returncode == 1
    assert "dosya yok" in (r.stdout + r.stderr)


def test_from_needs_select_drops_docstring_fp():
    """FROM-needs-SELECT filtresi: deploy.py gerçek SQL (SELECT FROM memories/sessions/
    tasks_log) yakalanmalı; docstring prose ('from tracking'/'from memory DB') ELENMELI."""
    r = _run("app/api/deploy.py")
    assert r.returncode == 0
    out = r.stdout
    for real in ("- memories", "- sessions", "- tasks_log"):
        assert real in out, f"gerçek tablo kayıp: {real}"
    # docstring-prose false-positive'leri olmamalı
    for fp in ("- tracking", "- memory\n", "- yaml", "- process"):
        assert fp not in out, f"docstring-FP sızdı: {fp!r}"


def test_diff_mode_empty_range():
    """--diff modu: değişiklik içermeyen range -> temiz exit 0 + 'değişen dosya yok'."""
    r = _run("--diff", "HEAD...HEAD")
    assert r.returncode == 0
    assert "değişen dosya yok" in (r.stdout + r.stderr)
