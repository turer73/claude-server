"""Crontab dogrudan-exec regresyon-guard'i (disc#1322/#1328-benzeri sinif, 2026-07-13).

klipper-cron-wrap.sh hedef-scripti `"$@"` ile DOGRUDAN calistirir (bash-prefix YOK) —
script'in exec (+x) biti eksikse cron rc=126 "Permission denied" ile sessizce fail eder
(sadece cron_outcomes/dashboard'da gorunur, PR/CI onceden yakalamaz). Canli-bulgu:
pattern_recognition.sh (disc#1322, 4-gun rc=126) + reflection.sh (ayni-gun kesfedildi,
henuz kendi discovery'si acilmamisti) — ikisi de PR#297/reflection-commit'inde 100644
olarak commit'lenmis. Bu test crontab'daki HER `.sh` referansini (bash/sh-prefix'siz)
tarar ve git-index mode'unun 100755 oldugunu dogrular.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CRONTAB = (REPO_ROOT / "automation" / "crontab").read_text(encoding="utf-8")

_SH_TOKEN_RE = re.compile(r"(\S+\.sh)\b")


def _git_mode(rel_path: str) -> str | None:
    """git-index'teki dosya modu (100755/100644). Tracked degilse None."""
    out = subprocess.run(
        ["git", "ls-files", "-s", rel_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return out.split()[0] if out else None


def _direct_exec_sh_paths() -> list[str]:
    """crontab'da bash/sh onek'i OLMADAN gecen /opt/linux-ai-server/*.sh path'leri."""
    paths = []
    for line in CRONTAB.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        for i, tok in enumerate(tokens):
            if not tok.endswith(".sh") or "/opt/linux-ai-server/" not in tok:
                continue
            prev = tokens[i - 1] if i > 0 else ""
            if prev in ("bash", "sh"):
                continue
            paths.append(tok.replace("/opt/linux-ai-server/", ""))
    return paths


def test_crontab_direct_exec_scripts_have_exec_bit():
    direct_exec = _direct_exec_sh_paths()
    assert direct_exec, "crontab'da dogrudan-exec .sh bulunamadi (parse regresyonu olabilir)"
    non_exec = [p for p in direct_exec if _git_mode(p) not in (None, "100755")]
    assert not non_exec, f"exec-biti eksik (git-index 100644), cron'da rc=126 verir: {non_exec}"
