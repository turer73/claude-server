"""Claude Code API — runs claude CLI with session persistence."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.middleware.dependencies import require_admin, require_auth

router = APIRouter(prefix="/api/v1/claude", tags=["claude-code"])

CLAUDE_BIN = os.path.expanduser("~/.npm-global/bin/claude")

# read_only=True (Telegram /claude) için SALT-OKUNUR araç allowlist'i. Plan-modu
# salt-okunur kabuğu da onaya takıyordu (git log çalışmıyordu) -> küratörlü allowlist:
# dosya-okuma + GÜVENLİ read-only kabuk. Mutasyon araçları (Edit/Write/yıkıcı Bash)
# listede YOK -> headless -p modunda otomatik reddedilir (prompt yok). NOT: allowedTools
# tam-sandbox değil (Claude Code doc); owner-only + mutasyon-yok katmanları. find/curl/
# xargs/sqlite3 (yazabilen/exec-eden) bilinçle HARİÇ.
READ_ONLY_ALLOWED_TOOLS = " ".join(
    [
        "Read",
        "Grep",
        "Glob",
        # git log/status: --output yazabilir (Codex P1) ama owner-only + disallow-mutasyon
        # katmanları; git diff/show/branch HARİÇ (diff/show --output write-vektörü, branch
        # -D/-m mutasyon). git status'ta --output yok (güvenli); git log en gerekli.
        "Bash(git log:*)",
        "Bash(git status:*)",
        # NOT: `journalctl` HARİÇ (Codex P1) — --vacuum-*/--rotate/--flush log SİLER/döndürür
        # ve prefix-disallow bunu güvenilir yakalayamaz (--vacuum-time=X / -u x --rotate
        # formları eşleşmez). systemd-journal /claude'dan ERİŞİLEMEZ; log-DOSYALARI Read +
        # `docker logs` (read-only) ile okunur. Yıkıcı-vektör güvenilir-kapatma > kapsam.
        "Bash(systemctl status:*)",
        "Bash(systemctl is-active:*)",
        "Bash(systemctl list-units:*)",
        "Bash(docker ps:*)",
        "Bash(docker logs:*)",
        "Bash(docker inspect:*)",
        "Bash(docker stats:*)",
        "Bash(df:*)",
        "Bash(du:*)",
        "Bash(free:*)",
        "Bash(ps:*)",
        "Bash(uptime:*)",
        "Bash(cat:*)",
        "Bash(head:*)",
        "Bash(tail:*)",
        "Bash(ls:*)",
        "Bash(wc:*)",
        "Bash(uname:*)",
        "Bash(sensors:*)",
        # data-analist: engine-zorlamalı read-only SQL (db-query.sh -readonly; yazma motorda
        # reddedilir, yalnız server/coverage alias). Ham sqlite3 HARİÇ kalır (DELETE riski).
        "Bash(/opt/linux-ai-server/scripts/db-query.sh:*)",
        "Bash(bash /opt/linux-ai-server/scripts/db-query.sh:*)",
    ]
)

# --allowedTools EKLEYİCİDİR (kısıtlayıcı değil): hesabın settings.json'ı ekstra araç
# izniyorsa allowlist tek başına read-only'yi GARANTİ ETMEZ (Codex P1). --disallowedTools
# EN YÜKSEK önceliklidir (settings + allow'u ezer) -> mutasyon araçlarını KESİN engelle.
# Dosya-mutasyon araçları + en yıkıcı kabuk komutları. (settings şu an boş ama
# enforcement gelecekteki/proje-settings'e karşı da dayanıklı olsun.)
READ_ONLY_DISALLOWED_TOOLS = " ".join(
    [
        "Edit",
        "Write",
        "NotebookEdit",
        "Bash(rm:*)",
        "Bash(rmdir:*)",
        "Bash(mv:*)",
        "Bash(dd:*)",
        "Bash(truncate:*)",
        "Bash(tee:*)",
        "Bash(chmod:*)",
        "Bash(chown:*)",
        "Bash(mkfs:*)",
        "Bash(kill:*)",
        "Bash(pkill:*)",
        # NOT: journalctl disallow-pattern'ları KALDIRILDI — prefix-eşleşme yıkıcı
        # bayrakları güvenilir yakalayamadığı için (Codex P1) journalctl tamamen
        # allowlist-DIŞI bırakıldı; sahte güvenlik vermesin.
    ]
)


def _load_claude_token():
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
    if os.path.exists(CLAUDE_BIN):
        return CLAUDE_BIN
    return shutil.which("claude")


def _build_env():
    env = {**os.environ}
    oauth = _load_claude_token()
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    # MAX-PLAN ZORUNLU (kullanıcı: "API istemiyorum"): API-key/auth-token env'i SİL ki
    # claude CLI her zaman abonelik kimliğine (~/.claude/.credentials.json veya OAuth
    # token) düşsün — pay-per-token API'ye ASLA. İleride .env/drop-in'e API-key sızsa
    # bile bu strip garanti sağlar.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _is_authenticated() -> bool:
    """Max-plan kimliği mevcut mu: OAuth token VEYA ~/.claude/.credentials.json
    (claude login). status authenticated=false yanıltıcıydı — token-env yoksa bile
    credentials dosyası ile run çalışır."""
    if _load_claude_token():
        return True
    return os.path.exists(os.path.expanduser("~/.claude/.credentials.json"))


def _parse_claude_json(raw: str, cwd: str, stderr_text: str = "", host: str = "klipper") -> dict[str, Any]:
    """CLI'dan donen JSON'i shape'e cevir. Hem lokal hem VPS yolu ayni.

    Claude Code bazen JSON'dan once kisa metin yaziyor (banner, hint),
    bu yuzden ilk { veya [ karakterinden itibaren parse'a basliyoruz.
    Cikti hem dict (--output-format json) hem list (jsonl stream) olabilir.
    """
    output = raw
    for i, ch in enumerate(raw):
        if ch in ("{", "["):
            output = raw[i:]
            break

    def _model_from_usage(d: dict[str, Any]) -> str | None:
        mu = d.get("modelUsage") or {}
        return next(iter(mu.keys()), None) if isinstance(mu, dict) else None

    try:
        result = json.loads(output)
        session_id = None
        answer = ""
        cost = 0
        is_error = False
        model = None

        if isinstance(result, dict):
            session_id = result.get("session_id")
            answer = result.get("result", "")
            cost = result.get("total_cost_usd", 0)
            is_error = result.get("is_error", False)
            model = _model_from_usage(result)
        elif isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    if item.get("type") == "result":
                        session_id = item.get("session_id")
                        answer = item.get("result", "")
                        cost = item.get("total_cost_usd", 0)
                        is_error = item.get("is_error", False)
                        model = _model_from_usage(item) or model
                    elif item.get("type") == "system" and item.get("session_id"):
                        session_id = item["session_id"]

        return {
            "ok": not is_error,
            "result": answer,
            "cost": cost,
            "session_id": session_id,
            "model": model,
            "cwd": cwd,
            "host": host,
        }
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw, "stderr": stderr_text, "host": host}


async def _run_on_vps(body: ClaudePromptRequest) -> dict[str, Any]:
    """VPS uzerinde claude'u SSH araciligiyla calistir.

    Klipper'in lokal CLI yolunun aynisi ama cmd uzaktan tetikleniyor.
    Sessions /root/.claude altinda saklaniyor — klipper session_id'leri
    burada gecerli degil; UI host degisirken session reset onerebilir.
    """
    settings = get_settings()
    if not settings.vps_host:
        return {"error": "VPS_HOST .env'de yapilandirilmamis"}

    # `--dangerously-skip-permissions` is rejected when claude runs as root
    # (security hard-stop in CLI), and the only login on VPS is root. Skip
    # the flag here; -p / --output-format json mode does not prompt anyway.
    args = ["claude", "-p", body.prompt, "--output-format", "json"]
    # read_only -> salt-okunur allowlist (VPS yolu da onurlandırır; Codex P2). skip-
    # permissions VPS'te zaten yok; allowlist read-only kabuğa izin + mutasyon reddi.
    if body.read_only:
        args.extend(["--allowedTools", READ_ONLY_ALLOWED_TOOLS, "--disallowedTools", READ_ONLY_DISALLOWED_TOOLS])
    if body.session_id:
        args.extend(["--resume", body.session_id])
    elif body.continue_last:
        args.append("--continue")
    if body.model:
        args.extend(["--model", body.model])
    if body.max_turns:
        args.extend(["--max-turns", str(body.max_turns)])

    cwd = body.cwd or "/root"
    inner = " ".join(shlex.quote(a) for a in args)
    remote = f"cd {shlex.quote(cwd)} && {inner}"
    # settings.vps_host zaten "user@host" formunda (.env'de boyle saklaniyor),
    # /api/v1/vps/exec'in de kullandigi convention. Olduğu gibi gec, prefix atma.
    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        settings.vps_host,
        remote,
    ]

    proc = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except TimeoutError:
        proc.kill()
        return {"error": "Zaman asimi (5dk)", "host": "vps"}

    raw = stdout.decode() if stdout else ""
    err = stderr.decode() if stderr else ""
    return _parse_claude_json(raw, cwd, err, host="vps")


class ClaudePromptRequest(BaseModel):
    prompt: str
    session_id: str | None = None  # Resume a previous session
    continue_last: bool = False  # Continue most recent session
    model: str | None = None
    max_turns: int | None = 10
    cwd: str | None = None
    # "klipper" runs the local CLI; "vps" sshes through to root@vps_host
    # and runs the same CLI there (sessions stored under /root/.claude/).
    # Sessions are NOT shared across hosts — switching host invalidates a
    # klipper-side session_id and vice versa; the UI surfaces this if it
    # cares to track host-per-session.
    host: str = "klipper"
    # read_only=True -> `--permission-mode plan` (salt-okunur: okur/analiz eder, komut
    # icra etmez / dosya değiştirmez). skip-permissions YERİNE. Telegram /claude bunu
    # kullanır (sınırsız-agent yüzeyi açma). Default False = mevcut web-UI davranışı.
    read_only: bool = False


@router.get("/status", dependencies=[Depends(require_auth)])
async def claude_status():
    binary = _find_claude()
    if not binary:
        return {"available": False, "error": "Claude Code CLI bulunamadi"}
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    version = stdout.decode().strip() if stdout else "unknown"
    # auth_method: subscription (Max-plan OAuth token / credentials.json) — API-key
    # _build_env'de strip edildiği için run her zaman abonelikten gider.
    if _load_claude_token():
        auth_method = "oauth_token"
    elif os.path.exists(os.path.expanduser("~/.claude/.credentials.json")):
        auth_method = "subscription_credentials"
    else:
        auth_method = "none"
    return {
        "available": True,
        "version": version,
        "authenticated": _is_authenticated(),
        "auth_method": auth_method,
        "binary": binary,
    }


@router.post("/run", dependencies=[Depends(require_admin)])
async def run_claude(body: ClaudePromptRequest):
    if body.host == "vps":
        return await _run_on_vps(body)

    binary = _find_claude()
    if not binary:
        return {"error": "Claude Code CLI bulunamadi"}

    # read_only -> salt-okunur allowlist (git log/journalctl gibi read-only kabuk ÇALIŞIR,
    # mutasyon reddedilir); değilse mevcut skip-permissions (web-UI). Telegram read_only=True.
    if body.read_only:
        # allow read-only + disallow mutasyon (disallow precedence -> settings'i ezer, P1).
        perm = ["--allowedTools", READ_ONLY_ALLOWED_TOOLS, "--disallowedTools", READ_ONLY_DISALLOWED_TOOLS]
    else:
        perm = ["--dangerously-skip-permissions"]
    cmd = [binary, "-p", body.prompt, "--output-format", "json", *perm]

    # Session continuity
    if body.session_id:
        cmd.extend(["--resume", body.session_id])
    elif body.continue_last:
        cmd.append("--continue")

    if body.model:
        cmd.extend(["--model", body.model])
    if body.max_turns:
        cmd.extend(["--max-turns", str(body.max_turns)])

    cwd = body.cwd or os.path.expanduser("~")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=_build_env(),
        stdin=asyncio.subprocess.DEVNULL,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except TimeoutError:
        proc.kill()
        return {"error": "Zaman asimi (5dk)"}

    raw = stdout.decode() if stdout else ""
    err = stderr.decode() if stderr else ""
    return _parse_claude_json(raw, cwd, err, host="klipper")


@router.get("/sessions", dependencies=[Depends(require_admin)])
async def list_sessions():
    """List Claude Code sessions across all projects.

    Reads `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` — the real
    session store as of Claude Code 2.x. The legacy `~/.claude/sessions`
    flat directory is no longer populated; the previous implementation
    scanned the wrong path and always returned a near-empty list.

    Project name is reconstructed by treating leading "-" + remaining "-"
    as "/" (e.g. "-opt-linux-ai-server" → "/opt/linux-ai-server"). Paths
    with literal dashes round-trip imperfectly but are rare in practice.
    """
    projects_root = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_root):
        return {"sessions": []}

    sessions = []
    for proj_dir in os.listdir(projects_root):
        proj_path = os.path.join(projects_root, proj_dir)
        if not os.path.isdir(proj_path):
            continue
        # Encoded dir uses "-" for both "/" and literal hyphens, so the
        # decoded path is ambiguous. If the naive decode points to a real
        # directory, prefer it; otherwise show the raw encoded form so
        # the user is not misled about what their project is called.
        if proj_dir.startswith("-"):
            decoded = "/" + proj_dir.lstrip("-").replace("-", "/")
            project = decoded if os.path.isdir(decoded) else proj_dir.lstrip("-")
        else:
            project = proj_dir
        for fname in os.listdir(proj_path):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(proj_path, fname)
            try:
                stat = os.stat(fpath)
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    line_count = sum(1 for _ in f)
                sessions.append(
                    {
                        "id": fname.removesuffix(".jsonl"),
                        "project": project,
                        "modified": stat.st_mtime,
                        "size_bytes": stat.st_size,
                        "lines": line_count,
                    }
                )
            except OSError:
                pass

    sessions.sort(key=lambda s: s["modified"], reverse=True)
    return {"sessions": sessions[:20]}


@router.get("/ui", dependencies=[Depends(require_auth)])
async def claude_ui():
    html_path = os.path.join(os.path.dirname(__file__), "..", "claude_ui", "index.html")
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Claude UI bulunamadi</h1>", status_code=404)
