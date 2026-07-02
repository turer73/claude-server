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
# 'diff --git a/X b/X' -> b-side path (mode-only/silme/++'siz durumda da dosya kaydi icin).
_DIFF_GIT_RE = re.compile(r"^diff --git a/.+ b/(.+)$")

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

    # UNION (Codex P2 #2): guard-turevi + fallback HER ZAMAN birlesir. Guard'da OLMAYAN
    # desenler (chmod-x-guard, MEMORY_API_KEY=, permissions.allow) yalniz fallback'te —
    # 'ya guard ya fallback' olsa parse-basarisinda bunlar kaybolurdu (design'da yikici sayilir).
    for name, rx in _FALLBACK_PATTERNS:
        try:
            compiled.append((name, re.compile(rx)))
        except re.error:
            continue
    return tuple(compiled)


def _parse_diff(git_diff: str) -> dict[str, dict[str, list[str]]]:
    """Unified-diff'i {path: {'added': [...], 'removed': [...]}} olarak ayristir.

    Dosya, 'diff --git a/X b/X' basligindan kaydedilir (Codex P2 #4/#deleted):
    - MODE-ONLY degisiklik (yalniz exec-bit) '+++' icermez -> baslik'tan yine 'touched' sayilir.
    - SILME '+++ /dev/null' -> baslik'tan gelen path + removed-lines KORUNUR (assertion-drop icin).
    Yalniz icerik satirlari sayilir; '+++'/'---'/@@ haric.
    """
    files: dict[str, dict[str, list[str]]] = {}
    cur: dict[str, list[str]] | None = None
    for line in git_diff.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            path = m.group(1) if m else None
            cur = files.setdefault(path, {"added": [], "removed": []}) if path else None
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            if p != "/dev/null":
                cur = files.setdefault(p, {"added": [], "removed": []})
            # '+++ /dev/null' (silme): diff --git'ten gelen cur'u KORU (path + removed-lines lazim)
        elif line.startswith("--- "):
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            if cur is not None:
                cur["added"].append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            if cur is not None:
                cur["removed"].append(line[1:])
    return files


def _count_assertions(lines: list[str]) -> int:
    return sum(len(_ASSERTION_RE.findall(ln)) for ln in lines)


def _is_comment_only(line: str) -> bool:
    """Satir SADECE-yorum mu (strip -> '#' veya '//' ile basliyor). Trailing-comment'li
    kod satiri (`x = 1  # ...`) yorum-only DEGIL — kod-kismi taranmali."""
    s = line.strip()
    return s.startswith("#") or s.startswith("//")


_ASSERTION_LINE_RE = re.compile(r"^(assert\b|self\.assert|await\s+expect\(|expect\(|pytest\.raises)")


def _is_assertion_line(line: str) -> bool:
    """Satir bir assertion/expectation MI (test-fixture/test-DATA). Test dosyasinda
    destructive-string tasisa bile benign (Codex #6); EXECUTABLE statement DEGIL."""
    return bool(_ASSERTION_LINE_RE.match(line.strip()))


def _is_trivial_assert(line: str) -> bool:
    """Tautology/trivial assertion mi (assert True/1/None, assert X==X, pass) — Codex #5.
    Bunlar 'added' sayilmaz: '-assert real()==42' + '+assert True' net=0 ile maskelenmesin."""
    s = line.strip()
    if s == "pass":
        return True
    if re.match(r"assert\s+(True|1|None)(\s+is\s+(True|None))?\s*$", s):
        return True
    m = re.match(r"assert\s+(.+?)\s*==\s*(.+?)\s*$", s)
    return bool(m and m.group(1) == m.group(2))


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def _is_guard_config(path: str) -> bool:
    return bool(_GUARD_CONFIG_RE.search(path))


def _module_stem(module: str) -> str:
    """Modul stem'i (test_ prefix / _test suffix soyulur). test_file fallback'inde
    'tests/test_foo.py' -> 'foo' => fix'lenen kaynak foo.py ile eslesir (Codex P2 #5)."""
    stem = Path(module).stem
    if stem.startswith("test_"):
        stem = stem[5:]
    elif stem.endswith("_test"):
        stem = stem[:-5]
    return stem


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

    # Sinyal 1: HER test dosyasinda GERCEK assertion-sayisi DUSTU mu (PER-DOSYA).
    #   Eklenen TRIVIAL/tautology assert'ler (assert True, assert x==x, pass) SAYILMAZ (Codex #5):
    #   '-assert compute()==42' + '+assert True' net=0 ile maskeleniyordu. Toplam-kiyas ayrica
    #   bir-testten-sil+baska-teste-trivial-ekle ile de maskelenebilirdi -> per-dosya (Codex P2).
    test_files = [p for p in files if _is_test_file(p)]
    dropped: dict[str, dict[str, int]] = {}
    for p in test_files:
        removed = _count_assertions(files[p]["removed"])
        added = sum(_count_assertions([ln]) for ln in files[p]["added"] if not _is_trivial_assert(ln))
        if removed - added > 0:
            dropped[p] = {"removed": removed, "added": added}
    if dropped:
        signals.append("test_assertion_drop")
        detail["assertion_delta"] = dropped

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
    #   TEST dosyalarinda (Codex #4, #6 revizyonu): fixture/assertion satiri (test-DATA,
    #   'assert guard_blocks("rm -rf")') benign ATLANIR AMA test-icine eklenen EXECUTABLE
    #   yikici-statement (os.system(rm -rf)) YINE taranir — 'tum-test-atla' cok-genisti.
    patterns = _load_destructive_patterns()
    hits: list[dict[str, str]] = []
    for path, blocks in files.items():
        in_test = _is_test_file(path)
        for ln in blocks["added"]:
            # Yorum-ONLY '+' satir = benign (aciklama, kod-degil; design 3 "bahsetmek!=yapmak").
            if _is_comment_only(ln):
                continue
            # Test dosyasi: yalniz assertion/fixture satiri benign; executable-statement taranir.
            if in_test and _is_assertion_line(ln):
                continue
            for name, rx in patterns:
                if rx.search(ln):
                    hits.append({"file": path, "pattern": name, "line": ln.strip()[:120]})
                    break  # satir basina tek hit yeter
    if hits:
        signals.append("destructive_pattern_added")
        detail["destructive_hits"] = hits

    return {"suspicious": bool(signals), "signals": signals, "detail": detail}
