"""CI auto-fixer — uses Claude Code CLI to fix failing tests.

Calls Claude Code with targeted prompts for each failure, re-runs tests,
and retries up to MAX_ATTEMPTS times.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import shutil
import uuid
from typing import Any

import httpx

from app.core.action_review import scan_ci_fixer_diff, soft_gate_enabled
from app.core.ci_runner import PROJECT_REGISTRY, run_project_tests
from app.core.ci_signal_dedup import (
    compute_signature,
    fetch_lesson_context,
    get_recent_occurrences,
    record_lesson,
)
from app.core.config import get_settings, read_env_var
from app.core.events import emit_event
from app.db.database import Database

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


async def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    """git -C cwd <args> -> (returncode, stdout, stderr). communicate() non-zero'da RAISE-ETMEZ."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        cwd,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


async def _snapshot_baseline(cwd: str) -> tuple[str, set[str]]:
    """Claude-fix ONCESI baseline: (tracked-baseline-ref, untracked-set). Codex #9:
    'git stash create' mevcut-WIP'i commit-object'e alir (working-tree DEGISMEZ); clean-tree'de
    bos -> 'HEAD'. Sonraki `git diff <baseline>` YALNIZ bu-attempt'in ekledigini gosterir
    (onceden-kirli dosyalarin attempt'e-atfedilme FP'si kalkar)."""
    _, sha, _ = await _git(cwd, "stash", "create")
    baseline = sha.strip()
    if not baseline:
        # Codex #255-P2(r3): clean-tree'de literal 'HEAD' donuyordu — HEAD HAREKETLI ref
        # (ci-fixer-settings `git checkout *`e izinli; repo-updater da oynatabilir). Run/attempt
        # ortasinda HEAD kayarsa `git diff HEAD` YENI checkout'a kiyas alip onceki gaming'i
        # gizler. Somut SHA'ya sabitle; rev-parse cokerse eski davranisa dus (degrade, daha-kotu-degil).
        rc, head_sha, _ = await _git(cwd, "rev-parse", "HEAD")
        baseline = head_sha.strip() if rc == 0 and head_sha.strip() else "HEAD"
    rc, others, _ = await _git(cwd, "ls-files", "--others", "--exclude-standard")
    pre_untracked = {f.strip() for f in others.splitlines() if f.strip()} if rc == 0 else set()
    return baseline, pre_untracked


def _difflib_git_diff(path: str, prior: str, cur: str) -> str:
    """prior->cur icin git-diff-formatinda unified diff (scanner bunu tarayabiliyor).
    Untracked dosyanin ONCEKI-attempt icerigine karsi diff'i icin — /dev/null-diff sadece
    '+' gosterir (silme gizli), bu ise gercek '-'/'+' verir (Codex #255-P2 r6 untracked-weaken)."""
    body = "".join(
        difflib.unified_diff(prior.splitlines(keepends=True), cur.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}")
    )
    return f"diff --git a/{path} b/{path}\n{body}"


async def _capture_attempt_diff(
    cwd: str,
    baseline_ref: str,
    pre_untracked: set[str],
    run_untracked_content: dict[str, str] | None = None,
) -> str:
    """baseline'dan-bu-yana tracked-delta (staged Claude-add dahil, Codex P1) + bu-attempt'te
    OLUSAN untracked dosyalar (Codex #3). `git diff <baseline>` non-zero => RuntimeError
    (caller fail-NOTIFY). Onceden-var untracked (pre_untracked) haric = yalniz attempt-delta.

    run_untracked_content (Codex #255-P2 r6, 574): run-boyunca-olusan untracked dosyalarin
    icerik-cache'i. Verilirse: ilk-gorulen untracked /dev/null'a (tum icerik '+'); SONRAKI
    gorusunde ONCEKI-icerige karsi difflenir (zayiflatma '-' olarak gorunur). Bu olmadan
    attempt-2'de dosya pre_untracked olup capture'dan dusuyor -> untracked test zayiflatma kacar."""
    rc, tracked, err = await _git(cwd, "diff", baseline_ref)
    if rc != 0:
        raise RuntimeError(f"git diff {baseline_ref} rc={rc}: {err.strip()[:200]}")
    parts = [tracked]
    rc2, others, _ = await _git(cwd, "ls-files", "--others", "--exclude-standard")
    if rc2 == 0:
        for line in others.splitlines():
            f = line.strip()
            if not f:
                continue
            if run_untracked_content is not None and (f in run_untracked_content or f not in pre_untracked):
                # run-ici olusan untracked: icerik-cache ile ONCEKI-attempt'e karsi izle.
                # KRITIK (Codex #255-P2 r6 / klipper #100322 prod-leak): guard'da
                # `f in run_untracked_content` SART. Gercek attempt_fix dongusunde pre_untracked
                # HER attempt'te _snapshot_baseline ile yeniden alinir; att-1'de olusan dosya
                # att-2'nin pre_untracked'INE girer -> yalniz `f not in pre_untracked` guard'i
                # att-2'de FALSE verip cache-branch'i atlar, zayiflatma KACAR. Cache'te olan dosya =
                # bu-run'da izledigimiz -> pre_untracked'te olsa bile izlemeye devam et.
                # (Cache'te-YOK + pre_untracked'te-VAR = gercek run-oncesi junk -> hala dislanir, FP yok.)
                try:
                    with open(os.path.join(cwd, f), errors="replace") as fh:
                        cur = fh.read()
                except OSError:
                    continue
                prior = run_untracked_content.get(f)
                run_untracked_content[f] = cur
                if prior is None:
                    # ilk gorus: yeni dosya, /dev/null (tum icerik '+')
                    _, d, _ = await _git(cwd, "diff", "--no-index", "--", "/dev/null", f)
                    parts.append(d)
                elif prior != cur:
                    # var olan run-ici untracked degisti: onceki-icerige karsi gercek diff
                    parts.append(_difflib_git_diff(f, prior, cur))
            elif f not in pre_untracked:
                # cache YOK (geri-uyum): mevcut davranis — /dev/null-diff.
                _, d, _ = await _git(cwd, "diff", "--no-index", "--", "/dev/null", f)
                parts.append(d)
    return "\n".join(p for p in parts if p)


async def _scan_fix_diff(
    cwd: str,
    baseline_ref: str,
    pre_untracked: set[str],
    source_file: str | None,
    test_file: str,
    project: str,
    test_name: str,
    run_untracked_content: dict[str, str] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Attempt-delta'yi yakala + spec-gaming tara (verdict-emit YOK — o _apply_review_verdict'te).

    KRITIK (Codex #255-P2): capture TESTLER KOSMADAN ONCE yapilmali — test-kosusunun mutate
    ettigi tracked dosyalar (yeni snapshot/golden/coverage-artifact) diff'e karisip sahte
    supheli-sinyal uretir (shadow-gozlem kirlenir; gate-ON'da temiz fix false-block yerdi).
    run_untracked_content: run-ici untracked izleme cache'i (r6 574 — bkz _capture_attempt_diff).
    Doner: (diff-metni, scan-result). (None, None) = capture/tarama-hatasi (fail-open;
    taranamadi-warn emit edilir). Diff-metni kumulatif birlesime de girer (Codex r4).
    """
    try:
        git_diff = await _capture_attempt_diff(cwd, baseline_ref, pre_untracked, run_untracked_content)
        return git_diff, scan_ci_fixer_diff(git_diff, failing_module=source_file or test_file)
    except Exception as exc:  # noqa: BLE001 — fail-NOTIFY, ci_fixer'i durdurma
        logger.warning("action_review taranamadi %s/%s: %s", project, test_name, exc)
        emit_event(
            type="action-review",
            source="ci_fixer",
            title=f"action-review taranamadi: {project}/{test_name}",
            severity="warn",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return None, None  # fail-open: tarama-hatasi ci_fixer'i bloklamaz


def _apply_review_verdict(
    result: dict[str, Any] | None,
    scope: str,
    project: str,
    test_name: str,
) -> bool:
    """Scan-sonucundan verdict cikar + supheliyse emit et.

    Doner: True = SOFT-GATE BLOK (supheli + gate-ON -> held-for-review), False = devam.
    Faz1 notify-only (gate-OFF): supheli-diff'te emit_event(warn), ci_fixer BLOKLAMAZ (False).
    Faz2 soft-gate (gate-ON): supheli-diff'te emit_event(CRITICAL) + True (accept-blok).
    result=None (tarama-hatasi) -> False (fail-open, pilot-tasarim 4).
    Kumulatif scope'ta EMIT pass-dalinda cagrilarak yapilir — suspicious+FAIL attempt'te
    sahte held-emit atilmaz (Codex #255-P2 emit-timing).
    """
    if result is None or not result["suspicious"]:
        return False

    scope_tag = " [kumulatif]" if scope == "cumulative" else ""
    gate_on = soft_gate_enabled()
    logger.warning("action_review SUPHELI%s %s/%s: %s (soft_gate=%s)", scope_tag, project, test_name, result["signals"], gate_on)
    emit_event(
        type="action-review",
        source="ci_fixer",
        title=("ci_fixer diff BLOK (held-for-review): " if gate_on else "ci_fixer diff supheli: ") + f"{project}/{test_name}{scope_tag}",
        severity="critical" if gate_on else "warn",
        detail="sinyaller: " + ", ".join(result["signals"]),
        payload={**result, "soft_gate": gate_on, "scope": scope},
    )
    return gate_on  # gate-ON -> True (blok); gate-OFF -> False (notify-only)


# P1#5: restricted Claude Code settings (filesystem/tool scope) used instead of
# --dangerously-skip-permissions. Repo-relative path; env-overridable.
CI_FIXER_SETTINGS = os.environ.get(
    "CI_FIXER_SETTINGS",
    os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "automation", "ci-fixer-settings.json")),
)

# Per-lesson fix_diff preview length in the context-enriched prompt. Kept well
# below app.core.ci_signal_dedup.FIX_DIFF_CAP (4096, the storage cap) so each
# past lesson stays lean in the model's context window while the DB row retains
# richer detail for later analysis.
FIX_DIFF_PROMPT_PREVIEW = 500

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")


async def _open_ci_db() -> Database:
    """Open a fresh Database connection for CI lesson recording.

    Tests monkeypatch this to return a tmp_path-scoped Database.
    """
    db = Database(get_settings().db_path)
    await db.initialize()
    return db


async def post_lesson_summary_to_memory_api(*, lesson_type: str, name: str, description: str, content: str) -> None:
    """Best-effort POST a lesson summary to the memory API.

    Silent on transport failure (network errors are caught and logged at
    warning level -- this helper must never raise into the fix loop).
    Non-2xx responses are logged at warning level with the status code and a
    truncated body so operators can diagnose rejected lessons (bad key,
    schema drift, upstream 5xx, etc.) instead of losing the signal silently.

    The wire JSON key is ``"type"`` -- the memory API contract. The Python
    parameter is named ``lesson_type`` to avoid shadowing the builtin.

    The memory API rejects payloads with backslash/newline characters in JSON
    (it uses a strict parser). As defense-in-depth this helper flattens any
    CR/LF in ``name``/``description``/``content`` to spaces before serializing,
    so a caller that interpolates an unsanitized value (e.g. a project slug
    with a stray newline, or a generative test name) cannot produce a payload
    the parser would silently reject.
    """
    try:
        settings = get_settings()
        base = settings.memory_api_base
        key = settings.memory_api_key
        if not base or not key:
            return

        def _flatten(s: str) -> str:
            return s.replace("\n", " ").replace("\r", " ")

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{base}/memories",
                headers={"X-Memory-Key": key, "Content-Type": "application/json"},
                json={"type": lesson_type, "name": _flatten(name), "description": _flatten(description), "content": _flatten(content)},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "memory api rejected payload: status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception as exc:
        logger.warning("memory api post failed: %s", exc)


# ---------------------------------------------------------------------------
# Claude Code helpers (same pattern as app/api/claude_code.py)
# ---------------------------------------------------------------------------


def _load_claude_token() -> str | None:
    """Load CLAUDE_CODE_OAUTH_TOKEN from env or dotfiles."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token
    for f in [os.path.expanduser("~/.claude_env"), os.path.expanduser("~/.bashrc")]:
        try:
            with open(f) as fh:
                for line in fh:
                    if "CLAUDE_CODE_OAUTH_TOKEN=" in line:
                        return line.split("=", 1)[1].strip().strip("'\"")
        except FileNotFoundError:
            pass
    return None


def _find_claude() -> str | None:
    """Find Claude Code CLI binary."""
    if os.path.exists(CLAUDE_BIN):
        return CLAUDE_BIN
    return shutil.which("claude")


def _build_env() -> dict[str, Any]:
    """Build environment dict with OAuth token."""
    env = {**os.environ}
    oauth = _load_claude_token()
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    return env


# ---------------------------------------------------------------------------
# Dedup env flag
# ---------------------------------------------------------------------------


def _dedup_enabled() -> bool:
    """Check whether CI signal dedup is enabled via env flag.

    Default: enabled. Set CI_SIGNAL_DEDUP_ENABLED=0 to disable.

    read_env_var (os.environ + .env-dosyasi fallback), os.environ.get DEGIL:
    systemd unit `.env`'i EnvironmentFile= ile gecirmiyor -> os.environ.get tek-basina
    `.env`'deki `=0` kill-switch'i goremez, gate serviste sessizce no-op kalir
    (#174 sinifi; bkz app/core/dead_gate.py). signal_quality.py:35 ile ayni desen.
    """
    return (read_env_var("CI_SIGNAL_DEDUP_ENABLED") or "1") != "0"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_fix_prompt(
    project: str,
    test_file: str,
    test_name: str,
    error: str,
    source_file: str | None = None,
    prev_errors: list[str] | None = None,
    context_lessons: list[dict[str, Any]] | None = None,
) -> str:
    """Build a prompt for Claude Code to fix a failing test.

    The prompt is in Turkish to match the existing codebase style.
    """
    lines = [
        f"Proje: {project}",
        f"Basarisiz test dosyasi: {test_file}",
        f"Test adi: {test_name}",
        "",
        "Hata mesaji:",
        error,
    ]

    if source_file:
        lines.extend(["", f"Ilgili kaynak dosya: {source_file}"])

    if prev_errors:
        lines.extend(["", "Onceki duzeltme denemelerinde alinan hatalar:"])
        for i, prev in enumerate(prev_errors, 1):
            lines.append(f"  Deneme {i}: {prev}")

    if context_lessons:  # truthy: non-None AND non-empty
        lines.extend(["", "Onceki denemelerdeki dersler (en yenisi ilk):"])
        # lowercase "deneme" (historical) visually distinguishes past-attempt
        # lessons (may be cross-run OR intra-run -- see
        # get_recent_occurrences docstring) from capital "Deneme"
        # (current-session retry) above.
        for lesson in context_lessons:
            # GAP-1 soft-gate pilot #6: held (supheli, insan-incelemede) ders "basarili ornek"
            # DEGIL — diff'i de GOSTERME (spec-gaming stratejisi promptla ogretilmesin).
            if lesson.get("outcome") == "held":
                lines.append(
                    f"  - deneme {lesson['attempt_num']} ({lesson['strategy']}) => held: "
                    f"SUPHELI diff (insan-incelemede) — bu yaklasimi ORNEK ALMA ({lesson['created_at']})"
                )
                continue
            lines.append(f"  - deneme {lesson['attempt_num']} ({lesson['strategy']}) => {lesson['outcome']} ({lesson['created_at']})")
            if lesson.get("fix_diff"):
                lines.append(f"    diff: {lesson['fix_diff'][:FIX_DIFF_PROMPT_PREVIEW]}")

    lines.extend(
        [
            "",
            "Bu testi duzelt. Sadece gerekli dosyalari degistir.",
            "Testi tekrar calistirdigimda gecmesini saglayacak en kucuk degisikligi yap.",
        ]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude Code caller
# ---------------------------------------------------------------------------


async def _call_claude_code(prompt: str, cwd: str) -> dict[str, Any]:
    """Call Claude Code CLI and return parsed result.

    Returns::

        {"answer": str, "session_id": str | None, "error": str | None}
    """
    binary = _find_claude()
    if not binary:
        return {"answer": "", "session_id": None, "error": "Claude Code CLI bulunamadi"}

    cmd = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        "5",
    ]
    # P1#5: scope filesystem/tools via a restricted settings file instead of
    # --dangerously-skip-permissions. The caller-supplied source_file in the
    # prompt could otherwise steer edits/reads outside the project (e.g. .env);
    # the settings deny-list blocks secrets + dangerous ops.
    if os.path.exists(CI_FIXER_SETTINGS):
        cmd += ["--settings", CI_FIXER_SETTINGS]
    else:
        logger.error("CI_FIXER_SETTINGS missing (%s) — aborting to prevent unrestricted execution", CI_FIXER_SETTINGS)
        return {"answer": "", "session_id": None, "error": f"CI_FIXER_SETTINGS dosyasi bulunamadi: {CI_FIXER_SETTINGS}"}

    logger.info("Calling Claude Code: cwd=%s", cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_build_env(),
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except TimeoutError:
        proc.kill()
        return {"answer": "", "session_id": None, "error": "Zaman asimi (5dk)"}
    except Exception as exc:
        return {"answer": "", "session_id": None, "error": str(exc)}

    raw = stdout.decode() if stdout else ""

    # Find JSON start (skip any preamble text)
    output = raw
    for i, ch in enumerate(raw):
        if ch in ("{", "["):
            output = raw[i:]
            break

    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        return {
            "answer": raw[:500],
            "session_id": None,
            "error": f"JSON parse hatasi. stderr: {stderr.decode()[:200] if stderr else ''}",
        }

    # Extract fields -- handle both dict and list formats
    session_id = None
    answer = ""

    if isinstance(result, dict):
        session_id = result.get("session_id")
        answer = result.get("result", "")
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                if item.get("type") == "result":
                    session_id = item.get("session_id")
                    answer = item.get("result", "")
                elif item.get("type") == "system" and item.get("session_id"):
                    session_id = item["session_id"]

    return {"answer": answer, "session_id": session_id, "error": None}


# ---------------------------------------------------------------------------
# Fix loop
# ---------------------------------------------------------------------------


async def attempt_fix(
    project: str,
    test_file: str,
    test_name: str,
    error: str,
    source_file: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Attempt to fix a failing test using Claude Code.

    Loops up to *max_attempts* times: builds prompt, calls Claude Code,
    re-runs tests, checks if the specific test passes.

    Returns::

        {
            "fixed": bool,
            "attempt": int,          # number of attempts made
            "project": str,
            "test_file": str,
            "test_name": str,
            "claude_responses": [...],
            "error": str | None,
        }
    """
    if project not in PROJECT_REGISTRY:
        return {
            "fixed": False,
            "attempt": 0,
            "project": project,
            "test_file": test_file,
            "test_name": test_name,
            "claude_responses": [],
            "error": f"Bilinmeyen proje: {project}",
        }

    run_uuid = uuid.uuid4().hex

    try:
        db = await _open_ci_db()
    except Exception as exc:
        logger.warning("CI lesson DB open failed: %s", exc)
        db = None

    try:
        cwd = PROJECT_REGISTRY[project]["path"]

        # GAP-1 soft-gate pilot #1 (Codex r4 revizyonu): kumulatif Claude-diff'i, her attempt'in
        # PRE-TEST delta'larinin BIRLESIMI olarak tutariz — run-basi-baseline'dan tek diff DEGIL.
        # Neden: run-baseline diff'i onceki attempt'lerin TEST-KOSUSU artifact'larini da
        # (snapshot/golden/coverage) icerir; runner-degisikligi ile Claude-degisikligini yapisal
        # olarak ayirt EDEMEZ. Her attempt-baseline onceki kosunun artifact'larini yuttugu icin
        # pre-test attempt-delta = SADECE Claude'un o attempt'te yaptigi -> birlesim runner-temiz.
        # Yan-etki (bilincli): attempt-1'de yapilip attempt-2'de GERI ALINAN gaming birlesimde
        # gorunur kalir — aldatici-gecmis tam da insan-gozune tasinmasi gereken sinyaldir.
        claude_deltas: list[str] = []
        # Codex r5: her attempt'in pre-test scan-sonucu (sticky supheli-tasima icin; index=attempt-1)
        attempt_scans: list[dict[str, Any] | None] = []
        # Codex r6 (574): run-boyunca-olusan untracked dosyalarin icerik-cache'i — sonraki
        # attempt'te ayni untracked dosya zayiflatilirsa onceki-icerige karsi diff (silme gorunur).
        run_untracked_content: dict[str, str] = {}

        prev_errors: list[str] = []
        claude_responses: list[dict[str, Any]] = []

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "attempt_fix: %s / %s -- deneme %d/%d",
                project,
                test_name,
                attempt,
                max_attempts,
            )

            # 0. Determine current error text and compute its signature ONCE.
            current_error_text = error if attempt == 1 else prev_errors[-1]
            error_hash, signature = compute_signature(
                project,
                test_name,
                current_error_text,
            )

            # 1. Dedup check: if the same signature failed in >=2 recent runs,
            #    switch to context-enriched strategy and fetch past lessons.
            strategy = "fix-direct"
            context_rows: list[dict[str, Any]] | None = None
            if _dedup_enabled() and db is not None:
                try:
                    recent = await get_recent_occurrences(db, signature, window=3)
                    if recent >= 2:
                        fetched = await fetch_lesson_context(
                            db,
                            project,
                            signature,
                            limit=5,
                        )
                        # Only flip the telemetry label when we actually have
                        # rows to enrich the prompt with -- otherwise the
                        # "context-enriched" strategy on the ci_lesson_learned
                        # row would diverge from the (empty) prompt context.
                        if fetched:
                            strategy = "context-enriched"
                            context_rows = fetched
                except Exception as exc:
                    logger.warning(
                        "dedup check failed, falling back to fix-direct: %s",
                        exc,
                    )
                    strategy = "fix-direct"
                    context_rows = None

            # 2. Build prompt (include previous errors for retries)
            prompt = build_fix_prompt(
                project=project,
                test_file=test_file,
                test_name=test_name,
                error=current_error_text,
                source_file=source_file,
                prev_errors=prev_errors if attempt > 1 else None,
                context_lessons=context_rows,
            )

            # 3. Baseline snapshot (Claude-ONCESI) + Call Claude Code
            baseline_ref, pre_untracked = await _snapshot_baseline(cwd)
            claude_result = await _call_claude_code(prompt, cwd)
            claude_responses.append(claude_result)

            # 3.5 GAP-1 action_review: PRE-EXECUTION denetim (attempt-delta; Codex #3/#9).
            #     Codex r6 (571): capture HATA OLSA DA yapilir — timeout/parse-hatasi veren Claude
            #     cagrisi working-tree'yi zaten degistirmis olabilir; yakalamazsak o zayiflatma
            #     sonraki attempt'in baseline'ina "temiz" girer (launder). Delta+scan biriktirilir
            #     (kumulatif+sticky'ye girer); sadece VERDICT/emit hatasiz-sonuca ozel degil —
            #     hata durumunda da supheli-diff insan-gozune tasinmali.
            claude_errored = bool(claude_result.get("error"))
            attempt_diff, attempt_scan = await _scan_fix_diff(
                cwd, baseline_ref, pre_untracked, source_file, test_file, project, test_name, run_untracked_content
            )
            held_for_review = _apply_review_verdict(attempt_scan, "attempt", project, test_name)
            if attempt_diff is not None:
                claude_deltas.append(attempt_diff)
            attempt_scans.append(attempt_scan)

            if claude_errored:
                logger.warning("Claude Code hatasi: %s", claude_result["error"])
                prev_errors.append(claude_result["error"])
                continue

            # 3.6 GAP-1 soft-gate pilot #1: KUMULATIF tarama — pre-test attempt-delta'larin
            # birlesimi uzerinde (Codex r2: capture pre-test; Codex r4: onceki attempt'lerin
            # runner-artifact'lari da birlesime GIREMEZ, cunku her delta kendi pre-test'inde
            # yakalandi). Attempt-delta review (3.5) tek basina laundering'i kacirir: attempt-1
            # supheli+FAIL tree'de kalir, attempt-2 temiz+PASS -> delta temiz gorunur; birlesim
            # gaming'i icerir.
            # Verdict+emit BURADA DEGIL — pass-dalinda (4.5): suspicious+FAIL'de sahte held-emit olmasin.
            # Fail-open: bir attempt'in delta'si yakalanamadiysa (None) birlesim eksik kalir —
            # tarama-hatasi accept'i bloklamaz (pilot-tasarim 4; taranamadi-warn 3.5'te atildi).
            cumulative_scan: dict[str, Any] | None = None
            if claude_deltas:
                try:
                    cumulative_scan = scan_ci_fixer_diff("\n".join(claude_deltas), failing_module=source_file or test_file)
                except Exception as exc:  # noqa: BLE001 — fail-open
                    logger.warning("kumulatif tarama hatasi %s/%s: %s", project, test_name, exc)
                    emit_event(
                        type="action-review",
                        source="ci_fixer",
                        title=f"action-review taranamadi: {project}/{test_name} [kumulatif]",
                        severity="warn",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    cumulative_scan = None

            # Codex #255-P2(r5): birlesim-scan TEK BASINA yetmez — attempt-1 assertion SILER,
            # attempt-2 AYNI dosyaya baska assertion EKLERSE birlesimde removed-added SIFIRA
            # netlesir, test_assertion_drop kaybolur (deneysel dogrulandi). Onceki attempt'lerin
            # pre-test scan'leri elimizde (FAIL'de bile kosuyor) -> supheli olanlari STICKY tasi:
            # kumulatif verdict = birlesim-scan VEYA onceki-attempt-scan supheli. (Guncel attempt
            # haric — onun suphesi held_for_review'da + kendi emit'i 3.5'te zaten var.)
            prior_suspicious = [(i, sc) for i, sc in enumerate(attempt_scans[:-1], 1) if sc and sc.get("suspicious")]
            if prior_suspicious:
                merged_signals = list(cumulative_scan.get("signals") or []) if cumulative_scan and cumulative_scan.get("suspicious") else []
                for i, sc in prior_suspicious:
                    merged_signals.extend(f"attempt{i}:{sig}" for sig in sc.get("signals", []))
                cumulative_scan = {"suspicious": True, "signals": merged_signals}

            # 4. Re-run tests
            test_result = await run_project_tests(project)
            tests_passed = test_result.get("failed", 0) == 0

            # 4.5 GAP-1 soft-gate pilot #1: kumulatif verdict accept-KAPISINDA (yalniz testler-gecti
            # dalinda, accept'ten ONCE; tarama 3.6'da pre-test delta-birlesiminde yapildi).
            # Fail-open: scan=None (tarama-hatasi) -> False, accept bloklanmaz (pilot-tasarim 4).
            cumulative_held = False
            if tests_passed:
                cumulative_held = _apply_review_verdict(cumulative_scan, "cumulative", project, test_name)
            effective_held = held_for_review or cumulative_held

            # Record the lesson for this attempt (before the passed/failed branches).
            # GAP-1 soft-gate pilot #6: held-fix 'passed' YAZILMAZ — lessons-DB'ye "basarili
            # spec-gaming ornegi" olarak girer, gelecek fixer'lar ogrenir (self-poison).
            outcome = "held" if (tests_passed and effective_held) else ("passed" if tests_passed else "failed")
            context_lessons_json = json.dumps([r["id"] for r in context_rows]) if context_rows else None
            if db is not None:
                try:
                    await record_lesson(
                        db,
                        run_uuid=run_uuid,
                        project=project,
                        test_name=test_name,
                        error_hash=error_hash,
                        signature=signature,
                        raw_error=current_error_text,
                        attempt_num=attempt,
                        strategy=strategy,
                        context_lessons=context_lessons_json,
                        fix_diff=claude_result.get("answer"),
                        outcome=outcome,
                        duration_ms=None,
                    )
                except Exception as exc:
                    logger.warning("lesson record failed: %s", exc)

            # 5. Check if fixed
            if tests_passed:
                # Faz2 SOFT-GATE (P3): supheli-diff + gate-ON -> fix'i ACCEPT ETME, held-for-review.
                # Testler gecse bile spec-gaming'le gecmis olabilir (assertion-zayiflatma) -> insan-review.
                # effective_held = attempt-delta (3.5) VEYA kumulatif accept-kapisi (4.5) blok'u.
                if effective_held:
                    held_scope = "attempt" if held_for_review else "cumulative"
                    logger.warning(
                        "action_review SOFT-GATE BLOK (%s): %s/%s testler-gecti ama supheli-diff -> held-for-review",
                        held_scope,
                        project,
                        test_name,
                    )
                    return {
                        "fixed": False,
                        "held_for_review": True,
                        "held_scope": held_scope,
                        "attempt": attempt,
                        "project": project,
                        "test_file": test_file,
                        "test_name": test_name,
                        "claude_responses": claude_responses,
                        "error": "action_review soft-gate: supheli-diff auto-accept bloklandi (insan-review)",
                    }
                logger.info("Test duzeltildi! deneme=%d", attempt)
                # (action_review 3.5'te testler-oncesi kosuldu — pre-execution gate)
                # Post summary to memory API (single-line content -- memory API rejects \n in JSON)
                await post_lesson_summary_to_memory_api(
                    lesson_type="lesson_learned",
                    name=f"CI fix: {project}/{test_name}",
                    description=f"Attempt {attempt}, {strategy} - fixed",
                    content=(
                        f"Run {run_uuid[:8]}: {test_name} in {project} fixed on attempt {attempt}. "
                        f"Signature: {signature}. "
                        f"Diff length: {len(claude_result.get('answer') or '')} chars."
                    ),
                )
                return {
                    "fixed": True,
                    "attempt": attempt,
                    "project": project,
                    "test_file": test_file,
                    "test_name": test_name,
                    "claude_responses": claude_responses,
                    "error": None,
                }

            # 6. Still failing -- collect error for next attempt
            current_error = "Bilinmeyen hata"
            for failure in test_result.get("failures", []):
                if failure.get("test_name") == test_name or failure.get("test_file") == test_file:
                    current_error = failure.get("error", current_error)
                    break
            else:
                # If exact match not found, take first failure error
                if test_result.get("failures"):
                    current_error = test_result["failures"][0].get("error", current_error)

            prev_errors.append(current_error)
            logger.info("Hala basarisiz, deneme=%d, hata=%s", attempt, current_error[:100])

        # Max attempts exhausted
        return {
            "fixed": False,
            "attempt": max_attempts,
            "project": project,
            "test_file": test_file,
            "test_name": test_name,
            "claude_responses": claude_responses,
            "error": f"{max_attempts} deneme sonrasi duzeltilemedi",
        }
    finally:
        if db is not None:
            try:
                await db.close()
            except Exception as exc:
                logger.warning("CI lesson DB close failed: %s", exc)
