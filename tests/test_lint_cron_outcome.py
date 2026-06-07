"""tools/lint-cron-outcome.sh — cron OUTCOME-contract lint'i (LIVESYS-SENSE).

Gerçek repo crontab'ında yeşil olmalı; fixture ile: OUTCOME'suz+allowlist'siz iş FAIL,
allowlist'li VEYA OUTCOME-emit-eden iş PASS.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINT = ROOT / "tools" / "lint-cron-outcome.sh"


def _run(env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(["bash", str(LINT)], capture_output=True, text=True, env=env)


def _fixture(tmp_path, script_body: str, allowlist: str = "") -> dict:
    """LINT_ROOT fixture: bir cron-wrap işi + sarılan script + allowlist kur."""
    (tmp_path / "automation").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "automation" / "job.sh").write_text(script_body)
    (tmp_path / "automation" / "crontab").write_text(
        "0 5 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh job /opt/linux-ai-server/automation/job.sh\n"
    )
    (tmp_path / "tools" / "allow.txt").write_text(allowlist)
    return {
        "LINT_ROOT": str(tmp_path),
        "LINT_CRONTAB": str(tmp_path / "automation" / "crontab"),
        "LINT_ALLOWLIST": str(tmp_path / "tools" / "allow.txt"),
    }


def test_lint_passes_on_real_repo():
    # Gerçek crontab + allowlist ile temiz olmalı (allowlist borcu kapsar).
    r = _run()
    assert r.returncode == 0, f"repo lint FAIL:\n{r.stdout}\n{r.stderr}"


def test_lint_flags_missing_outcome(tmp_path):
    env = _fixture(tmp_path, "#!/bin/bash\necho merhaba\n")  # OUTCOME yok, allowlist boş
    r = _run(env)
    assert r.returncode == 1
    assert "OUTCOME marker yok" in r.stderr


def test_lint_allowlist_suppresses(tmp_path):
    env = _fixture(tmp_path, "#!/bin/bash\necho merhaba\n", allowlist="job.sh\n")
    r = _run(env)
    assert r.returncode == 0
    assert "ALLOW" in r.stdout


def test_lint_passes_when_script_emits_outcome(tmp_path):
    env = _fixture(tmp_path, '#!/bin/bash\necho "OUTCOME: pass | tamam"\n')
    r = _run(env)
    assert r.returncode == 0


def test_lint_flags_comment_only_outcome(tmp_path):
    # Codex P2: '# TODO OUTCOME:' gerçek emit DEĞİL → lint geçirmemeli (sessiz-green engeli).
    env = _fixture(tmp_path, "#!/bin/bash\n# TODO OUTCOME: ekle\necho merhaba\n")
    r = _run(env)
    assert r.returncode == 1
    assert "OUTCOME marker yok" in r.stderr
