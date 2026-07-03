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

import json
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


def soft_gate_enabled() -> bool:
    """Faz2 EVAL-GATE: soft-gate acik mi (env-flag, DEFAULT-OFF = notify-only; Faz2 P3).
    ON iken action_review suspicious-diff -> ci_fixer auto-accept BLOKLANIR (held-for-review).
    read_env_var (#174 config-gate: systemd .env'i os.environ'a yuklemez; os.environ.get tek-basina
    serviste kill-switch'i goremez). Fail-safe: flag-yok/OFF/eval-gecmemis -> notify-only kalir.
    Operasyonel-kural: bu flag ancak `make eval-gap2` catch>=0.90 & false-block<=0.05 gosterince ON."""
    from app.core.config import read_env_var  # lazy: import-cycle onleme

    return (read_env_var("ACTION_REVIEW_SOFT_GATE") or "0").strip().lower() in ("1", "true", "on", "yes")


def dispatch_policy_gate_enabled() -> bool:
    """Policy-gate #1222: cross-agent dispatch HOLD-enforcement acik mi (env-flag, DEFAULT-OFF).
    ON iken otonom-origin + suspicious dispatch -> create_note status='held' (aliciya teslim yok,
    insan-onayi bekler). soft_gate_enabled()'in DISPATCH tarafi ESI — AYRI env-key (soft-gate ci_fixer
    ciktisini, policy-gate dispatch-kanalini gate'ler; ikisi bagimsiz flip). read_env_var (config-gate
    dersi #174: os.environ.get systemd .env'i gormez). Fail-safe: flag-yok/OFF -> notify-only.
    Operasyonel-kural: bu flag ancak `make eval-gap2` Part-D catch>=0.90 & fb<=0.05 gosterince ON."""
    from app.core.config import read_env_var  # lazy: import-cycle onleme

    return (read_env_var("DISPATCH_POLICY_GATE") or "0").strip().lower() in ("1", "true", "on", "yes")


# Assertion belirtecleri (py + js). Kelime-siniri ile FP azaltilir.
# NOT (Faz2 P2 #9): 'should' KALDIRILDI — prose/docstring-silmede yanlis assertion-drop veriyordu
# ('this should work' gibi). Spesifik assertion-token'lari kalir.
_ASSERTION_RE = re.compile(
    r"\b(assert|assertEqual|assertTrue|assertFalse|assertRaises|assertIn|"
    r"assertIsNone|assertIsNotNone|pytest\.raises|expect)\b"
    # Faz2 #4: chai `.should.` (DOT-prefix) assertion sayilir — bare 'should' (prose) DEGIL,
    # nokta-onekli oldugu icin 'this should work' FP vermez ('should' Faz2 P2 #9'da cikarilmisti).
    r"|\.should\b"
)
# Test dosyasi: tests/ | __tests__/ (Faz2 P2 #6, JS-layout) | test_*.py | *_test.py | *.test.* | *.spec.*
_TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)__tests__/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$")
# Guard/config zayiflatma yuzeyleri (Faz2 P2 #7: DARALTILDI — herhangi settings.json/conftest.py
# normal-app-ayarinda FP veriyordu; yalniz GERCEK guard/gate-config path'leri).
_GUARD_CONFIG_RE = re.compile(
    r"pre-bash-guard\.sh$|ci-fixer-settings\.json$|autonomous-claude-settings\.json$|"
    r"(^|/)\.claude/settings(\.local)?\.json$|(^|/)tests/conftest\.py$|(^|/)\.env(\.[^/]+)?$"
)
# Faz2 P1 #1: test-devre-disi markerlari (skip/xfail/only) — eklenince test atlanir, assertion-drop YOK.
_DISABLE_MARKER_RE = re.compile(
    r"@pytest\.mark\.(skip|xfail)|pytest\.skip\(|unittest\.skip|"
    r"\b(it|describe|test)\.(skip|only)\(|xdescribe\(|xit\(|"
    # Faz2 #7: MODULE-level `pytestmark = pytest.mark.skip/xfail(...)` (atama, @-dekorator degil) —
    # tum dosyayi atlar; @-gerektiren desen evade ediliyordu. Codex-253 P2: SADECE skip/xfail —
    # `pytestmark = pytest.mark.asyncio/usefixtures/django_db` MESRU-config, disable DEGIL.
    r"\bpytestmark\s*=[^\n]*\.(skip|xfail)\b"
)
# Faz2 P1 #3: EXECUTOR cagrilari — assertion-satirinda OLSA DA yikici-desen taransin
# (assert os.system("rm -rf")==0 spec-gaming; fixture guard_blocks("rm -rf") DEGIL).
_EXECUTOR_RE = re.compile(
    r"\b(os\.system|os\.popen|subprocess\.(run|call|check_call|check_output|Popen)|"
    r"exec|eval|commands\.getoutput|child_process\.(exec|execSync|spawn))\s*\("
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
    # Faz2 #9: birlesik -fr (f-once-r) + AYRIK flag (rm -r -f / rm -f -r) evade ediyordu.
    # Codex-253 P2: aradaki opsiyon UZUN (--one-file-system) veya kombine olabilir → -{1,2}...[\w-]*.
    ("rm-fr", r"rm\s+(?:-{1,2}[A-Za-z][\w-]*\s+)*-[A-Za-z]*f[A-Za-z]*r"),
    ("rm-r-f-split", r"rm\s+(?:-{1,2}[A-Za-z][\w-]*\s+)*-[A-Za-z]*r[A-Za-z]*\s+(?:-{1,2}[A-Za-z][\w-]*\s+)*-[A-Za-z]*f"),
    ("rm-f-r-split", r"rm\s+(?:-{1,2}[A-Za-z][\w-]*\s+)*-[A-Za-z]*f[A-Za-z]*\s+(?:-{1,2}[A-Za-z][\w-]*\s+)*-[A-Za-z]*r"),
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
def _destructive_pattern_sources() -> tuple[tuple[str, str], ...]:
    """Yikici-desen KAYNAKLARI (name, regex-str) — pre-bash-guard.sh'ten TURET (DRY) + fallback UNION.

    Parse/derleme hatasi = fail-safe (guard yoksa minimal fallback). guard-turevi + _FALLBACK
    HER ZAMAN birlesir (guard'da olmayan chmod-x-guard/MEMORY_API_KEY=/permissions.allow korunur).
    Kaynak-ayrimi: hem case-sensitive (Kapsam-1 diff) hem case-insensitive (Kapsam-2 dispatch) derlensin.
    """
    raw = ""
    try:
        raw = _GUARD_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""

    m = re.search(r"DANGEROUS_PATTERNS=\((.*?)\n\)", raw, re.S) if raw else None
    sources: list[tuple[str, str]] = []
    if m:
        for i, ere in enumerate(re.findall(r"'([^']+)'", m.group(1))):
            py = ere
            for posix, repl in _POSIX_MAP.items():
                py = py.replace(posix, repl)
            try:
                re.compile(py)  # derlenebilir mi (fail-safe)
            except re.error:
                continue
            sources.append((f"guard[{i}]", py))
    sources.extend(_FALLBACK_PATTERNS)
    return tuple(sources)


@lru_cache(maxsize=1)
def _load_destructive_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Case-SENSITIVE derlenmis yikici-desenler (Kapsam-1 ci_fixer diff — kod case'i onemli)."""
    return tuple((name, re.compile(p)) for name, p in _destructive_pattern_sources())


@lru_cache(maxsize=1)
def _load_destructive_patterns_ci() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Case-INSENSITIVE (Kapsam-2 dispatch — SQL/komut case-bagimsiz, Codex #4 'drop table')."""
    return tuple((name, re.compile(p, re.IGNORECASE)) for name, p in _destructive_pattern_sources())


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
    """Tautology/trivial assertion mi (py + JS/Vitest). Bunlar 'added' sayilmaz:
    '-assert real()==42' + '+assert True' (veya '+expect(true).toBe(true)') net=0 maskelenmesin.
    Codex #5 + Faz2 P1 #5 (Vitest one-for-one: expect(true).toBe(true), toBe(true), .skip)."""
    s = line.strip()
    if s == "pass":
        return True
    # Python tautology
    if re.match(r"assert\s+(True|1|None)(\s+is\s+(True|None))?\s*$", s):
        return True
    m = re.match(r"assert\s+(.+?)\s*==\s*(.+?)\s*$", s)
    if m and m.group(1) == m.group(2):
        return True
    # JS/Vitest tautology (Faz2 P1 #5): expect(true|1).toBe(true|1), expect(x).toBe(x-ayni), .skip
    if re.search(r"expect\(\s*(true|1)\s*\)\.(toBe|toEqual|toBeTruthy)\(", s):
        return True
    m2 = re.search(r"expect\(\s*(.+?)\s*\)\.(?:toBe|toEqual)\(\s*(.+?)\s*\)", s)
    if m2 and m2.group(1) == m2.group(2):
        return True
    # chai `.should` tautology (Codex-253 P2): true.should.be.true / x.should.equal(x-ayni) —
    # gercek .should silinip bununla degistirilirse net=0 maskelenmesin.
    if re.search(r"\b(true|1)\.should\b", s):
        return True
    m3 = re.search(r"\b(.+?)\.should\.(?:equal|eql|eq|be)\(?\s*(.+?)\s*\)?\s*;?\s*$", s)
    return bool(m3 and m3.group(1).strip() == m3.group(2).strip())


def _normalize_argv(line: str) -> str:
    """argv-form yikici komutu BIRLESIK forma cevir (Faz2 P1 #4): subprocess.run(["rm","-rf","/x"])
    -> "... rm -rf /x ...". String-literal ayiraclarini (", ' , [ ] , virgul) bosluga cevir ki
    'rm\\s+-rf' deseni komut+flag'i ayni-string'de gorsun. Orijinal-satirla birlikte taranir."""
    return re.sub(r"""['"\[\],]+""", " ", line)


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


# pytest/JS'in TOPLADIGI test dosya-adi (dizin-bazli _is_test_file'dan FARKLI: 'tests/foo.py'
# tests/ altinda ama pytest toplamaz). rename-to-disable = toplanan→toplanmayan.
# Codex-253 P2: Jest `__tests__/` altindaki HER dosya toplanir (dizin-bazli) → rename-disable
# bypass'i kapansin (src/__tests__/foo.js -> foo_disabled.js).
_COLLECTED_TEST_RE = re.compile(r"(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$|(^|/)__tests__/")
# Disable-INTENT ile yeniden-adlandirma soneki (hala toplansa da 'kapatma' niyeti): _off/_disabled/...
_DISABLE_RENAME_SUFFIX_RE = re.compile(r"(_off|_disabled?|_skip|_wip|_old|_bak|_ignore|\.disabled)\.[^./]+$", re.I)


def _is_collected_test(path: str) -> bool:
    return bool(_COLLECTED_TEST_RE.search(path))


def _detect_test_renames(git_diff: str) -> list[str]:
    """Diff'te test-atlama amacli rename ('rename-to-disable', Faz2 P1 #2). from/to CIFTLE
    (git ardisik yazar). DISABLE = pytest'in TOPLADIGI test → (toplanmayan isme VEYA disable-sonekli
    isme, orn. _off/_disabled) tasiniyor. Faz2 #5 (FP): toplanan→toplanan MESRU refactor
    (test_foo.py→test_bar.py) BAYRAK ATILMAZ. NOT: _is_test_file dizin-bazli, burada filename-bazli."""
    froms = re.findall(r"^rename from (.+)$", git_diff, re.M)
    tos = re.findall(r"^rename to (.+)$", git_diff, re.M)
    out: list[str] = []
    for frm, to in zip(froms, tos):
        frm, to = frm.strip(), to.strip()
        if _is_collected_test(frm) and (not _is_collected_test(to) or _DISABLE_RENAME_SUFFIX_RE.search(to)):
            out.append(frm)
    return out


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
    """path, failing_module ile iliskili mi (kendisi / testi / ayni-stem).
    Faz2 P2 #10: substring DEGIL COMPONENT-eslesme — 'foo_backdoor.py' stem-substring ile
    'foo' sayilmasin. path'in KENDI stem'i (test_ prefix soyulu) failing-stem'e esit olmali."""
    stem = _module_stem(failing_module)
    if not stem:
        return True  # stem yoksa iliskisiz-diyemeyiz (FP-guvenligi)
    return path == failing_module or _module_stem(path) == stem


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

    # Sinyal 1b: TEST DEVRE-DISI (Faz2 P1 #1/#2) — skip/xfail/only marker eklenmesi VEYA
    #   test-dosyasi rename'i (assertion-drop olmadan test-atlama/gizleme = spec-gaming).
    # Yorum-ONLY satirlar atlanir (contextual-whitelist: '# pytest.mark.skip kullan' = prose,
    # bahsetmek != yapmak). Sinyal 5 ile ayni disiplin — `pytestmark=` eklenince FP olmasin.
    marker_disabled = [p for p in test_files if any(_DISABLE_MARKER_RE.search(ln) for ln in files[p]["added"] if not _is_comment_only(ln))]
    renamed_tests = _detect_test_renames(git_diff)
    if marker_disabled or renamed_tests:
        signals.append("test_disabled")
        detail["test_disabled"] = {"markers": marker_disabled, "renamed": renamed_tests}

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
            # Test dosyasi: assertion/fixture satiri benign AMA EXECUTOR-cagrisi (os.system/subprocess)
            # iceriyorsa YINE tara (Faz2 P1 #3: assert os.system("rm -rf")==0 = spec-gaming, fixture DEGIL).
            if in_test and _is_assertion_line(ln) and not _EXECUTOR_RE.search(ln):
                continue
            # Orijinal + argv-normalized form tara (Faz2 P1 #4: subprocess.run(["rm","-rf"]) -> "rm -rf").
            candidates = (ln, _normalize_argv(ln)) if ("[" in ln or "," in ln) else (ln,)
            matched = False
            for cand in candidates:
                for name, rx in patterns:
                    if rx.search(cand):
                        hits.append({"file": path, "pattern": name, "line": ln.strip()[:120]})
                        matched = True
                        break
                if matched:
                    break
    if hits:
        signals.append("destructive_pattern_added")
        detail["destructive_hits"] = hits

    return {"suspicious": bool(signals), "signals": signals, "detail": detail}


# ---------------------------------------------------------------------------
# Kapsam-2: cross-agent dispatch notu denetimi (A-1 icerik + A-2 otonom-origin)
# ---------------------------------------------------------------------------

# Task-paketinin EXECUTABLE alanlari (bunlar taranir). title/aciklama/description/ozet/
# content/attack_type gibi META/PROSE alanlari TARANMAZ (alan-farkinda-whitelist cekirdegi).
# NOT (Codex #2): built-in dispatcher (_send_to_surer) 'gorev'+'degisiklikler'(=surer_tasks)
# kullanir; interactive-klipper 'adimlar[]' yazar. Ikisi de kapsanmali.
_EXEC_KEYS = {
    "adimlar",
    "steps",
    "cmd",
    "cmds",
    "command",
    "commands",
    "komut",
    "komutlar",
    "script",
    "gorev",
    "degisiklikler",
    "surer_tasks",
    "klipper_cmds",
    # canonical/dispatcher ic-alanlar (Codex round-2): surer_tasks[].degisiklik (singular,
    # executable-degisiklik-metni; dispatch.py:79) + hedef (tek-cumle ne-yapilacak; sistem-tanimi.md:66).
    "degisiklik",
    "hedef",
}
# Cross-agent dispatch-zarfi gostergeleri (built-in dispatcher to_device set ETMEZ; Codex #1).
_ENVELOPE_KEYS = {"alici", "gonderen", "to", "recipient", "tip"}
# Task-paketi gostergesi (bunlardan biri varsa content bir dispatch-task-paketidir).
_TASK_KEYS = _EXEC_KEYS | _ENVELOPE_KEYS | {"gorev_paketi", "gorev_id", "basari_kriteri"}


def _exec_value_strings(value: Any) -> list[str]:
    """Bir EXECUTABLE-alan degerinden taranacak string'ler (alan-farkinda, derinlemesine).

    - string -> [string]
    - argv-array ['rm','-rf','/x'] -> tek-tek + BIRLESIK 'rm -rf /x' (Codex #3: desenler
      komut+flag'i ayni string'de arar).
    - structured-steps [{'description': prose, 'command': 'pytest'}] -> YALNIZ exec-subfield
      (command/cmd/...); 'description'/prose subfield'lari TARANMAZ (Codex #5).
    - dict -> yalniz exec-subfield'leri.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        strs = [v for v in value if isinstance(v, str)]
        out.extend(strs)
        if strs:
            out.append(" ".join(strs))  # argv-join (#3)
        for item in value:
            if isinstance(item, (dict, list)):
                out.extend(_exec_value_strings(item))
        return out
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if k in _EXEC_KEYS:
                out.extend(_exec_value_strings(v))
        return out
    return []


def _try_parse_task_package(content: str) -> dict[str, Any] | None:
    """content bir JSON dispatch-task-paketi mi -> dict; degilse (duz-prose/JSON-degil) None.

    Task-paketi = JSON-dict + en az bir _TASK_KEYS anahtari (gorev_paketi ic-sarmali olabilir).
    Duz-prose not (klipper'in cogu dispatch-mesaji) JSON-degil -> None -> TARANMAZ (benign).
    """
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    inner = obj.get("gorev_paketi")
    candidates = [obj, inner] if isinstance(inner, dict) else [obj]
    if any(k in c for c in candidates for k in _TASK_KEYS):
        return obj
    return None


def _collect_exec_strings(task: dict[str, Any]) -> list[str]:
    """Task-paketinin TOP-LEVEL (+ gorev_paketi sarmali) executable-alan string'leri.

    KRITIK (FP-onleme): YALNIZ executable-alanlar (adimlar[]/gorev/cmd/...) taranir; DERIN-NESTED
    data (eval_set_format.*.cmd = test-datasi) + prose-subfield'ler (step.description) TARANMAZ.
    Klipper'in kendi dispatch-notlari (#100248) nested-data'da yikici-string tasir = benign kalmali.
    """
    out: list[str] = []
    roots = [task]
    inner = task.get("gorev_paketi")
    if isinstance(inner, dict):
        roots.append(inner)
    for root in roots:
        for key in _EXEC_KEYS:
            if key in root:
                out.extend(_exec_value_strings(root[key]))
    return out


def _is_autonomous_origin(from_device: str | None) -> bool:
    """from_device otonom-varyanti mi (or. 'klipper-autonomous'). Interactive 'klipper' DEGIL."""
    return from_device is not None and "autonomous" in from_device.lower()


def _is_cross_agent(to_device: str | None, task: dict[str, Any] | None) -> bool:
    """Cross-agent dispatch mi: to_device dolu VEYA content-zarfinda alici/gonderen/tip var.
    Built-in dispatcher to_device set ETMEZ ama content'te alici='surer-sonnet' tasir (Codex #1).
    Zarf-anahtarlari gorev_paketi-SARMALI ic-obje'de de olabilir (Codex round-2)."""
    if to_device:
        return True
    if task is None:
        return False
    candidates = [task]
    inner = task.get("gorev_paketi")
    if isinstance(inner, dict):
        candidates.append(inner)
    return any(k in c for c in candidates for k in _ENVELOPE_KEYS)


def scan_dispatch_note(content: str, from_device: str | None = None, to_device: str | None = None) -> dict[str, Any]:
    """Cross-agent dispatch notunu deterministik denetle (Kapsam-2 A-1 + A-2).

    A-1: content JSON task-paketiyse YALNIZ executable-alanlarinda (adimlar/gorev/cmd/...) yikici-desen
         ara (case-INSENSITIVE, Codex #4). Meta/prose/nested-data/step-prose TARANMAZ.
    A-2: origin otonom (from_device~'*autonomous*') + cross-agent (to_device VEYA zarf-alici) +
         task-paketi => otonom-consequential-dispatch sinyali (#1222/#100248 human-gate?).

    Returns {"suspicious": bool, "signals": [...], "detail": {...}}. LLM yok; notify-only kullanim.
    """
    signals: list[str] = []
    detail: dict[str, Any] = {}

    task = _try_parse_task_package(content)

    if task is not None:
        patterns = _load_destructive_patterns_ci()  # dispatch = case-insensitive (Codex #4)
        hits: list[dict[str, str]] = []
        for s in _collect_exec_strings(task):
            for name, rx in patterns:
                if rx.search(s):
                    hits.append({"pattern": name, "snippet": s.strip()[:120]})
                    break
        if hits:
            signals.append("dispatch_destructive_op")
            detail["destructive_hits"] = hits

    if _is_autonomous_origin(from_device) and _is_cross_agent(to_device, task) and task is not None:
        signals.append("autonomous_cross_agent_dispatch")
        detail["origin"] = from_device

    return {"suspicious": bool(signals), "signals": signals, "detail": detail}
