"""OUTCOME-marker lint — klipper-cron-wrap ile calisan her script OUTCOME: basmali.

klipper-cron-wrap.sh script ciktisinda `OUTCOME: pass|partial|fail | detay` satiri
bekler; yoksa cron_outcomes'a "outcome-undefined (no OUTCOME marker)" yazilir ve
gercek sonuc gorunmez olur (disc#1288: autonomous-health her-4h sahte-fail,
daily-backup pass/fail ayrimsiz).

Ratchet modeli (mypy-ratchet gibi): mevcut eksikler KNOWN_MISSING allowlist'te —
YENI eklenen cron-wrapped script OUTCOME'suz ise test kirilir; allowlist'teki bir
script duzeltilirse listeden cikarilmasi zorunlanir (bayat allowlist birikmez).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CRONTAB = REPO / "automation" / "crontab"

# Wrap'in runtime regex'i: ^OUTCOME:\s*(pass|partial|fail)  (scripts/klipper-cron-wrap.sh).
# Statik kaynak-taramasi runtime-cikti goremez; bu yuzden kabul edilen KAYNAK formlari:
#   - literal keyword:  echo "OUTCOME: pass | ..."   /  print(f"OUTCOME: pass | ...")
#   - degisken sonuc:   echo "OUTCOME: $r | ..."     (pull-vps-backup, demo-reset-test)
#   - f-string sonuc:   print(f"OUTCOME: {r} | ...")
# 'OUTCOME: artifact-cleanup OK' gibi keyword'suz literaller ESLESMEZ (PR#299 Codex#2:
# duz substring-match bunlari yanlis-gecirip wrapper'da outcome-undefined birakiyordu).
OUTCOME_SRC_RE = re.compile(r"OUTCOME: *(pass|partial|fail|\$|\{)")

# Ratchet-borcu: bu scriptler henuz OUTCOME basmiyor (2026-07-10 denetimi, disc#1288).
# Yeni OUTCOME eklenen script buradan CIKARILMALI — test aksi halde kirilir.
KNOWN_MISSING = {
    "agent-health-report.sh",
    "autonomous-daily-summary.sh",
    "claude-code-update.sh",
    "cross_source_consolidation.sh",
    "data-analyst.sh",
    "digest-send.sh",
    "intent-liveness-audit.sh",
    "memory-synthesize.sh",
    "meta_cognition.sh",
    "pattern_recognition.sh",
    "predictive_agent.sh",
    "rag-reindex.sh",
    "reflection.sh",
    "self_improvement.sh",
    "seo-audit.sh",
    "seo-ctr-watch.sh",
    "system-state.sh",
    "weekly-audit.sh",
}


def cron_wrapped_scripts():
    """crontab'daki klipper-cron-wrap.sh satirlarindan hedef script yollarini cikar.

    Satir formati: <schedule> [ENV=..] .../klipper-cron-wrap.sh <job-adi> <komut...>
    Komut kismindaki ilk /opt/linux-ai-server/**.sh|.py yolu hedef script'tir
    (venv/bin/python3 gibi interpreter onekleri es gecilir).
    """
    for line in CRONTAB.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "klipper-cron-wrap.sh" not in line:
            continue
        m = re.search(r"klipper-cron-wrap\.sh\s+\S+\s+(.+)$", line)
        if not m:
            continue
        for tok in m.group(1).split():
            if tok.startswith("/opt/linux-ai-server/") and tok.endswith((".sh", ".py")):
                yield tok
                break


def _repo_path(abs_path: str) -> Path:
    return REPO / abs_path.removeprefix("/opt/linux-ai-server/")


def test_cron_wrapped_scripts_exist_in_repo():
    missing = [p for p in set(cron_wrapped_scripts()) if not _repo_path(p).is_file()]
    assert not missing, f"crontab'da referansli ama repoda olmayan script: {sorted(missing)}"


def test_outcome_marker_ratchet():
    scripts = sorted(set(cron_wrapped_scripts()))
    assert scripts, "crontab'dan hic cron-wrapped script cikarilamadi (parse bozuk?)"

    new_missing, stale_allowlist = [], []
    for abs_path in scripts:
        f = _repo_path(abs_path)
        if not f.is_file():
            continue  # varlik ayri testte
        has_outcome = bool(OUTCOME_SRC_RE.search(f.read_text(encoding="utf-8", errors="replace")))
        if not has_outcome and f.name not in KNOWN_MISSING:
            new_missing.append(f.name)
        if has_outcome and f.name in KNOWN_MISSING:
            stale_allowlist.append(f.name)

    assert not new_missing, (
        f"cron-wrapped script OUTCOME: basmiyor — wrap 'outcome-undefined' kaydeder, gercek sonuc kaybolur: {new_missing}"
    )
    assert not stale_allowlist, f"KNOWN_MISSING bayat — bunlar artik OUTCOME iceriyor, allowlist'ten cikar: {stale_allowlist}"
