#!/usr/bin/env python3
"""
Stop hook — Claude oturumu bitince transcript'i ozetleyip Memory API'ye
session olarak yazar. Yari otonomi icin kritik: oturum bilgisi ucup gitmez.

Stdin: {"session_id":"...", "transcript_path":"...", "stop_hook_active":bool, ...}
Cikti: bos. Hook sessiz calisir, hatalari /opt/.../hook-logs/hooks.log'a yazar.
"""
from __future__ import annotations
import json, os, sys, socket, time, urllib.request, urllib.error
from pathlib import Path

HOOK_NAME = "stop-save-session"
LOG_DIR = Path(os.environ.get("HOOK_LOG_DIR", "/opt/linux-ai-server/data/hook-logs"))
API_BASE = os.environ.get("HOOK_API", "http://127.0.0.1:8420/api/v1/memory")
ENV_FILE = Path(os.environ.get("HOOK_ENV_FILE", "/opt/linux-ai-server/.env"))
DEVICE = os.environ.get("HOOK_DEVICE") or socket.gethostname()
MIN_TURNS = int(os.environ.get("HOOK_MIN_TURNS", "2"))   # Bu kadar user mesaji yoksa kaydetme
MAX_FILES = 30
MAX_SUMMARY_LEN = 2000


def log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "hooks.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{HOOK_NAME}] {msg}\n")
    except Exception:
        pass


def load_api_key() -> str:
    key = os.environ.get("MEMORY_API_KEY", "")
    if key:
        return key
    if ENV_FILE.is_file():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("MEMORY_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except Exception as e:
            log(f"env read fail: {e}")
    return ""


def parse_transcript(path: Path) -> dict:
    """JSONL transcript'i tarayip ozetle."""
    user_prompts: list[str] = []
    files_changed: set[str] = set()
    bash_commands: list[str] = []
    test_results: list[tuple[str, int]] = []  # (cmd, exit_code)
    git_commits: list[str] = []
    last_assistant_text = ""

    if not path.is_file():
        log(f"transcript yok: {path}")
        return {}

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rtype = rec.get("type") or rec.get("role") or ""

                # Kullanici prompt'lari
                if rtype in ("user", "human"):
                    msg = rec.get("message") or rec
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if isinstance(content, str):
                        user_prompts.append(content[:500])
                    elif isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                user_prompts.append((blk.get("text") or "")[:500])

                # Asistan blogu — son metni tut, tool_use'lari topla
                if rtype in ("assistant",):
                    msg = rec.get("message") or {}
                    for blk in (msg.get("content") or []):
                        if not isinstance(blk, dict):
                            continue
                        bt = blk.get("type")
                        if bt == "text":
                            txt = blk.get("text") or ""
                            if txt:
                                last_assistant_text = txt[:1000]
                        elif bt == "tool_use":
                            name = blk.get("name", "")
                            inp = blk.get("input") or {}
                            if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                                fp = inp.get("file_path") or inp.get("notebook_path")
                                if fp:
                                    files_changed.add(fp)
                            elif name == "Bash":
                                cmd = (inp.get("command") or "").strip()
                                if cmd:
                                    bash_commands.append(cmd[:200])
                                    if "git commit" in cmd:
                                        git_commits.append(cmd[:200])

                # Tool result — exit code yakala
                if rtype in ("user",) or rec.get("toolUseResult"):
                    res = rec.get("toolUseResult") or {}
                    if isinstance(res, dict):
                        rc = res.get("exit_code")
                        if rc is None:
                            rc = res.get("returncode")
                        if rc is not None and bash_commands:
                            test_results.append((bash_commands[-1], rc))
    except Exception as e:
        log(f"transcript parse fail: {e}")
        return {}

    return {
        "user_prompts": user_prompts,
        "files_changed": sorted(files_changed)[:MAX_FILES],
        "bash_commands": bash_commands[-50:],
        "test_results": test_results[-20:],
        "git_commits": git_commits,
        "last_assistant_text": last_assistant_text,
    }


def build_summary(parsed: dict, cwd: str) -> str:
    parts = []
    prompts = parsed.get("user_prompts") or []
    if prompts:
        first = prompts[0].strip().replace("\n", " ")[:200]
        parts.append(f"hedef: {first}")
    n_files = len(parsed.get("files_changed") or [])
    if n_files:
        parts.append(f"degisen dosya: {n_files}")
    n_cmds = len(parsed.get("bash_commands") or [])
    if n_cmds:
        parts.append(f"bash: {n_cmds}")
    fails = [r for r in (parsed.get("test_results") or []) if r[1] not in (0, "0")]
    if fails:
        parts.append(f"FAIL test: {len(fails)}")
    commits = parsed.get("git_commits") or []
    if commits:
        parts.append(f"commit: {len(commits)}")
    if cwd:
        parts.append(f"cwd: {os.path.basename(cwd)}")
    last = (parsed.get("last_assistant_text") or "").replace("\n", " ").strip()[:300]
    summary = " | ".join(parts)
    if last:
        summary += f"\n\nson durum: {last}"
    return summary[:MAX_SUMMARY_LEN]


def post_session(api_base: str, key: str, payload: dict) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{api_base}/sessions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Memory-Key": key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read().decode("utf-8", errors="ignore")
            log(f"session POST ok: {data[:200]}")
            return True
    except urllib.error.HTTPError as e:
        log(f"session POST HTTP {e.code}: {e.read().decode(errors='ignore')[:200]}")
    except Exception as e:
        log(f"session POST fail: {e}")
    return False


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(f"stdin parse fail: {e}")
        return

    if data.get("stop_hook_active"):
        # Hook icinden tetiklenen Stop — sonsuz dongu olmasin
        return

    transcript_path = data.get("transcript_path") or ""
    cwd = data.get("cwd") or os.getcwd()
    parsed = parse_transcript(Path(transcript_path)) if transcript_path else {}

    prompts = parsed.get("user_prompts") or []
    if len(prompts) < MIN_TURNS:
        log(f"skip: kullanici turn sayisi {len(prompts)} < {MIN_TURNS}")
        return

    key = load_api_key()
    if not key:
        log("MEMORY_API_KEY yok, session kaydedilmedi")
        return

    summary = build_summary(parsed, cwd)
    payload = {
        "device_name": DEVICE,
        "summary": summary,
        "tasks_completed": [p[:120] for p in prompts[:10]],
        "files_changed": parsed.get("files_changed") or [],
        "notes": f"auto-saved by stop-hook | transcript: {transcript_path}",
    }

    post_session(API_BASE, key, payload)


if __name__ == "__main__":
    main()
