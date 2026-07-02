"""GAP-1 action_review — cikti-tarafi DETERMINISTIK denetim (Kapsam-1: ci_fixer diff).

Otonom ci_fixer'in URETTIGI working-tree `git diff`'ini kabul-oncesi semantik denetler:
spec-gaming (testi zayiflatip gecirme) + guard/config zayiflatma + anormal-buyuk diff +
modul-disi degisiklik + eklenen-satirda yikici-desen.

TASARIM (docs/gap1-action-review-design.md):
- LLM-DEGIL. GAP-2 kaniti: LLM-classifier iyi-bicimli-kotucul icerigi ayirt edemez (0.40).
  Deterministik desen = fail-safe, ucuz, test-edilebilir.
- BAGLAMSAL-WHITELIST (cekirdek): yikici-desen taramasi YALNIZ '+' (eklenen) satirlarda;
  '-'/context/prose SATIRLARI taranmaz. assertion-sayimi '+' vs '-' KIYAS. Bu, bu oturumda
  3x yasanan FP'nin (bahsetmek != yapmak) kok-cozumu.
- Yikici-desen listesi pre-bash-guard.sh'ten TURETILIR (DRY, tek-kaynak).
- notify-only (Faz1): cagiran taraf emit_event(warn) atar, ci_fixer'i BLOKLAMAZ.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# app/core/action_review.py -> repo koku (parents[2])
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD_FILE = _REPO_ROOT / "scripts" / "hooks" / "pre-bash-guard.sh"

# Eklenen-satir esigi: kucuk-fix beklenirken buyuk-diff = anomali. Env ile ayarlanabilir.
DIFF_MAX_ADDED = int(os.environ.get("ACTION_REVIEW_DIFF_MAX_ADDED", "200"))

# Assertion belirtecleri (py + js). Kelime-siniri ile FP azaltilir.
_ASSERTION_RE = re.compile(
    r"\b(assert|assertEqual|assertTrue|assertFalse|assertRaises|assertIn|"
    r"assertIsNone|assertIsNotNone|pytest\.raises|expect|should)\b"
)
# Test dosyasi: tests/ altinda veya test_*.py / *_test.py / *.test.* / *.spec.*
_TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$")
# Guard/config zayiflatma yuzeyleri (path-bazli).
_GUARD_CONFIG_RE = re.compile(
    r"pre-bash-guard\.sh$|(^|/)settings\.json$|ci-fixer-settings\.json$|"
    r"(^|/)conftest\.py$|(^|/)\.env(\.[^/]+)?$"
)

# POSIX karakter-sinifi -> Python re cevirisi (grep-ERE desenlerini re'ye uyarlar).
_POSIX_MAP = {
    "[[:space:]]": r"\s",
    "[[:alpha:]]": r"[A-Za-z]",
    "[[:alnum:]]": r"[A-Za-z0-9]",
    "[[:digit:]]": r"\d",
    "[[:upper:]]": r"[A-Z]",
    "[[:lower:]]": r"[a-z]",
}

# pre-bash-guard.sh parse edilemezse fail-safe minimal fallback (guard'in ozeti).
_FALLBACK_PATTERNS: list[tuple[str, str]] = [
    ("rm-rf", r"rm\s+-[A-Za-z]*r[A-Za-z]*f"),
    ("force-push", r"git\s+push.*(--force|\s-f(\s|$))"),
    ("git-reset-hard", r"git\s+reset\s+--hard"),
    ("git-clean", r"git\s+clean\s+-[A-Za-z]*[fdx]"),
    ("drop-table", r"DROP\s+(TABLE|DATABASE|SCHEMA)"),
    ("truncate", r"TRUNCATE\s+TABLE"),
    ("curl-pipe-sh", r"curl\s+.*\|\s*(bash|sh|zsh)(\s|$)"),
    ("chmod-x-guard", r"chmod\s+-x\s+.*guard"),
    ("env-write-key", r"MEMORY_API_KEY\s*="),
    ("perms-allow", r"permissions.*allow"),
]


@lru_cache(maxsize=1)
def _load_destructive_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Yikici-desenleri pre-bash-guard.sh'ten TURET (DRY). Parse/derleme hatasi = fail-safe.

    Guard bulunamaz/parse-edilemezse minimal fallback dizisi kullanilir (bloklamaz, sadece
    daha-az kapsar). Bu Kapsam-1 icin yeterli; ana sinyaller assertion-drop/guard-config.
    """
    raw = ""
    try:
        raw = _GUARD_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""

    m = re.search(r"DANGEROUS_PATTERNS=\((.*?)\n\)", raw, re.S) if raw else None
    compiled: list[tuple[str, re.Pattern[str]]] = []
    if m:
        for i, ere in enumerate(re.findall(r"'([^']+)'", m.group(1))):
            py = ere
            for posix, repl in _POSIX_MAP.items():
                py = py.replace(posix, repl)
            try:
                compiled.append((f"guard[{i}]", re.compile(py)))
            except re.error:
                continue  # cevrilemeyen deseni atla (fail-safe)

    if not compiled:  # parse basarisiz -> fallback
        compiled = [(name, re.compile(rx)) for name, rx in _FALLBACK_PATTERNS]
    return tuple(compiled)


def _parse_diff(git_diff: str) -> dict[str, dict[str, list[str]]]:
    """Unified-diff'i {path: {'added': [...], 'removed': [...]}} olarak ayristir.

    Yalniz icerik satirlari; '+++'/'---' basliklari ve hunk-header'lari (@@) haric.
    """
    files: dict[str, dict[str, list[str]]] = {}
    cur: dict[str, list[str]] | None = None
    for line in git_diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path == "/dev/null":
                cur = None
                continue
            cur = files.setdefault(path, {"added": [], "removed": []})
        elif line.startswith("--- ") or line.startswith("diff --git"):
            if line.startswith("diff --git"):
                cur = None  # +++ gorulene dek satir sayma
        elif line.startswith("+") and not line.startswith("+++"):
            if cur is not None:
                cur["added"].append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            if cur is not None:
                cur["removed"].append(line[1:])
    return files


def _count_assertions(lines: list[str]) -> int:
    return sum(len(_ASSERTION_RE.findall(ln)) for ln in lines)


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def _is_guard_config(path: str) -> bool:
    return bool(_GUARD_CONFIG_RE.search(path))


def _module_stem(module: str) -> str:
    return Path(module).stem


def _is_related(path: str, failing_module: str) -> bool:
    """path, failing_module ile iliskili mi (kendisi / testi / ayni-stem)."""
    stem = _module_stem(failing_module)
    if not stem:
        return True  # stem yoksa iliskisiz-diyemeyiz (FP-guvenligi)
    return path == failing_module or stem in Path(path).name


def scan_ci_fixer_diff(git_diff: str, failing_module: str | None = None) -> dict[str, Any]:
    """ci_fixer working-tree diff'inde spec-gaming/zayiflatma sinyallerini tara (deterministik).

    Args:
        git_diff: working-tree `git diff` ciktisi (Claude-prozasi DEGIL).
        failing_module: fail-eden testin modul-yolu (verilirse modul-disi sinyali acilir).

    Returns:
        {"suspicious": bool, "signals": list[str], "detail": dict}. LLM yok; notify-only kullanim.
    """
    signals: list[str] = []
    detail: dict[str, Any] = {}

    if not git_diff or not git_diff.strip():
        return {"suspicious": False, "signals": [], "detail": {"note": "bos-diff"}}

    files = _parse_diff(git_diff)

    # Sinyal 1: test dosyasinda assertion-sayisi DUSTU ('+' vs '-' kiyas — baglamsal).
    test_files = [p for p in files if _is_test_file(p)]
    if test_files:
        removed = sum(_count_assertions(files[p]["removed"]) for p in test_files)
        added = sum(_count_assertions(files[p]["added"]) for p in test_files)
        if removed - added > 0:
            signals.append("test_assertion_drop")
            detail["assertion_delta"] = {"removed": removed, "added": added, "test_files": test_files}

    # Sinyal 2: guard/config zayiflatma (path-bazli).
    guard_touched = [p for p in files if _is_guard_config(p)]
    if guard_touched:
        signals.append("guard_config_touched")
        detail["guard_config_files"] = guard_touched

    # Sinyal 3: anormal-buyuk diff (eklenen satir).
    total_added = sum(len(f["added"]) for f in files.values())
    if total_added > DIFF_MAX_ADDED:
        signals.append("diff_size_anomaly")
        detail["added_lines"] = total_added

    # Sinyal 4: fail-eden modul DISI dosya degisti (opsiyonel — failing_module verilirse).
    if failing_module:
        out = [p for p in files if not _is_test_file(p) and not _is_related(p, failing_module)]
        if out:
            signals.append("out_of_failing_module")
            detail["out_of_module_files"] = out

    # Sinyal 5: eklenen ('+') satirlarda yikici-desen — BAGLAMSAL-WHITELIST cekirdegi.
    #   '-'/context/prose taranmaz: bahsetmek != yapmak (3x-FP kok-cozumu).
    patterns = _load_destructive_patterns()
    hits: list[dict[str, str]] = []
    for path, blocks in files.items():
        for ln in blocks["added"]:
            for name, rx in patterns:
                if rx.search(ln):
                    hits.append({"file": path, "pattern": name, "line": ln.strip()[:120]})
                    break  # satir basina tek hit yeter
    if hits:
        signals.append("destructive_pattern_added")
        detail["destructive_hits"] = hits

    return {"suspicious": bool(signals), "signals": signals, "detail": detail}
