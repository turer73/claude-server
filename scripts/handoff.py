#!/usr/bin/env python3
"""Cross-device handoff: Klipper (Opus) <-> Windows (Sonnet) via shared claude_memory.db.

Subscription discipline: only interactive Claude Code sessions on each side.
Headless `-p` mode forbidden (charges the API). Coordination via notes API + scp.

Subcommands:
  send   Upload a prompt to remote Windows + write a note that tells Sonnet what to do.
  wait   Block until either: (a) the expected return file appears locally, or (b) a
         new note arrives from a Windows device. Use as `wait <return_path>`.
  check  List unread notes on Klipper, optionally filtered by --from-dev / --since.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

API = "http://localhost:8420/api/v1/memory"
ENV_PATH = "/opt/linux-ai-server/.env"
SELF_DEVICE = "klipper"
WINDOWS_DEVICES = {"surer", "windows-masaustu", "turer", "windows-laptop"}

# Tailscale/SSH machine name -> canonical memory-DB device name.
# The watcher on Windows filters notes by to_device using the canonical name
# (windows-masaustu / windows-laptop), not the Tailscale alias (surer / turer),
# so notes addressed via Tailscale alias were silently missed (note #39).
TAILSCALE_TO_DEVICE = {
    "surer": "windows-masaustu",
    "windows-masaustu": "windows-masaustu",
    "turer": "windows-laptop",
    "windows-laptop": "windows-laptop",
}

# Sonnet 4.6 hard limits (observed from claude.exe headless test 2026-05-10):
#   context_window=200_000  maxOutputTokens=32_000
SONNET_OUTPUT_HARD_LIMIT = 32_000
# Practical safety threshold — leaves buffer for stop tokens, retries, partial outputs.
# Above this we recommend splitting. (Bilge English A2 batch used ~11.5K output for 50 items
# — so ~25K accommodates ~100 items of similar density.)
SAFE_OUTPUT_THRESHOLD = 25_000
# Rough char-per-token. Mixed English+Turkish+JSON ≈ 3.3. Conservative for our use:
CHARS_PER_TOKEN = 3.3

# Per-device Windows username (different from Tailscale machine name)
DEVICE_USER = {
    "surer": "sevdi",
    "windows-masaustu": "sevdi",
    "turer": "turgu",
    "windows-laptop": "turgu",
}


def get_key() -> str:
    for line in Path(ENV_PATH).read_text().splitlines():
        if line.startswith("MEMORY_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("MEMORY_API_KEY not in .env")


def api_get(path: str, key: str):
    req = urllib.request.Request(f"{API}{path}", headers={"X-Memory-Key": key})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def api_post(path: str, key: str, payload: dict):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Memory-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def estimate_input_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def estimate_output_tokens(prompt_text: str, target_items: int | None, avg_tokens_per_item: int) -> int:
    """Estimate output tokens. Two strategies:

    1) If --target-items given: target_items * avg_tokens_per_item.
    2) Else, scan prompt for an explicit count hint ("exactly N items", "Output N lines").
    """
    import re

    if target_items is None:
        m = re.search(
            r"(?:exactly|tam|total|toplam|Output|output)\s+(\d+)\s+(?:items|item|satir|lines|line|JSONL)", prompt_text, re.IGNORECASE
        )
        if m:
            target_items = int(m.group(1))
    if target_items is None:
        # No hint — return a conservative "unknown" sentinel via 0
        return 0
    return target_items * avg_tokens_per_item


def assess_split(output_est: int) -> tuple[str, int]:
    """Return (status, recommended_batches). status: OK | SPLIT_2 | SPLIT_3 | SPLIT_N."""
    if output_est <= 0:
        return ("UNKNOWN", 1)
    if output_est <= SAFE_OUTPUT_THRESHOLD:
        return ("OK", 1)
    n = -(-output_est // SAFE_OUTPUT_THRESHOLD)  # ceil
    return (f"SPLIT_{n}", n)


def cmd_estimate(args):
    prompt_text = Path(args.prompt).read_text()
    inp = estimate_input_tokens(prompt_text)
    out = estimate_output_tokens(prompt_text, args.target_items, args.avg_tokens_per_item)
    status, n = assess_split(out)
    print(f"prompt_chars={len(prompt_text)}")
    print(f"input_tokens_est={inp}")
    if out > 0:
        print(f"output_tokens_est={out}  (target_items × avg_tokens_per_item)")
    else:
        print("output_tokens_est=UNKNOWN  (no count hint in prompt; pass --target-items)")
    print(f"sonnet_output_hard_limit={SONNET_OUTPUT_HARD_LIMIT}")
    print(f"safe_threshold={SAFE_OUTPUT_THRESHOLD}")
    print(f"status={status}")
    if n > 1:
        per_batch = -(-args.target_items // n) if args.target_items else "?"
        print(f"recommendation: split into {n} batches × {per_batch} items each")
    return 0


def remote_paths(to_dev: str):
    if to_dev not in DEVICE_USER:
        sys.exit(f"unknown device: {to_dev} (must be one of {sorted(WINDOWS_DEVICES)})")
    u = DEVICE_USER[to_dev]
    scp_dir = f"/Users/{u}/AppData/Local/Temp"
    win_dir = f"C:\\Users\\{u}\\AppData\\Local\\Temp"
    return scp_dir, win_dir


def cmd_send(args):
    to = args.to
    project = args.project
    prompt_file = Path(args.prompt).resolve()
    if not prompt_file.exists():
        sys.exit(f"prompt file not found: {prompt_file}")

    # Token sanity check (fail-loud if user asked for more than safe threshold)
    prompt_text = prompt_file.read_text()
    inp_est = estimate_input_tokens(prompt_text)
    out_est = estimate_output_tokens(prompt_text, args.target_items, args.avg_tokens_per_item)
    status, n = assess_split(out_est)
    print(f"[token-est] input≈{inp_est}  output≈{out_est or 'UNKNOWN'}  status={status}")
    if status.startswith("SPLIT_") and not args.force:
        print(f"[BLOCK] estimated output {out_est} > safe threshold {SAFE_OUTPUT_THRESHOLD}", file=sys.stderr)
        print(f"[BLOCK] split into {n} batches and re-send each, or pass --force to ignore", file=sys.stderr)
        sys.exit(3)

    scp_dir, win_dir = remote_paths(to)
    fname = prompt_file.name
    remote_win = f"{win_dir}\\{fname}"
    remote_scp = f"{scp_dir}/{fname}"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_fname = args.output_name or f"{project}_{ts}.out"
    out_win = f"{win_dir}\\{out_fname}"
    out_scp = f"{scp_dir}/{out_fname}"

    return_dir = Path(args.return_dir) if args.return_dir else Path(f"/home/klipperos/handoffs/{project}")
    return_dir.mkdir(parents=True, exist_ok=True)
    return_path = str(return_dir / out_fname)

    subprocess.run(["scp", "-q", str(prompt_file), f"{to}:{remote_scp}"], check=True)

    canonical_to = TAILSCALE_TO_DEVICE[to]
    title = args.title or f"{project}: {prompt_file.stem}"
    content = (
        f"PROJECT: {project}\n"
        f"TASK_TYPE: {args.task_type}\n"
        f"PROMPT_PATH: {remote_win}\n"
        f"OUTPUT_PATH: {out_win}\n"
        f"RETURN_TO: klipper:{return_path}\n\n"
        f"ADIMLAR:\n"
        f"1. Bu oturumda model claude-sonnet-4-6 olmali (Max plan dahilinde). API call/headless -p mode YASAK.\n"
        f"2. Read {remote_win}\n"
        f"3. Prompt talimatlarini uygula, ciktiyi {out_win}'e yaz.\n"
        f"4. scp {out_win} klipper:{return_path}\n"
        f"5. Donus notu: POST {API}/notes from_device={canonical_to} to_device=klipper "
        f'title="{project} done" content="path={return_path} | summary=<kac satir, dist, vs>"\n'
        f"6. Bu note (id={{this_id}}) read=1 isaretle: PUT {API}/notes/<id>/read\n\n"
        f"KURAL: Sadece interactive Max subscription oturum. Headless `-p` ya da `--api-key` ile cagri YASAK "
        f"(charge eder, kullanici net yasakladi)."
    )

    resp = api_post(
        "/notes",
        get_key(),
        {
            "from_device": SELF_DEVICE,
            "to_device": canonical_to,
            "title": title,
            "content": content,
        },
    )

    print(f"note_id={resp['id']}")
    print(f"to={to} (canonical={canonical_to})  project={project}")
    print(f"prompt_uploaded={remote_scp}")
    print(f"expected_output_remote={out_scp}")
    print(f"return_path={return_path}")
    print("")
    print("# arm watcher:")
    print(f"python3 {sys.argv[0]} wait {return_path}")


def cmd_wait(args):
    target = Path(args.path)
    key = get_key()
    start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    interval = max(5, args.interval)
    deadline = time.time() + args.timeout if args.timeout > 0 else None

    while True:
        if target.exists():
            print(f"FILE_ARRIVED:{target}")
            return 0
        try:
            data = api_get("/notes?device=klipper&unread_only=true", key)
            new_notes = [n for n in data if n.get("from_device") in WINDOWS_DEVICES and n.get("created_at", "") > start]
            if new_notes:
                n = new_notes[0]
                print(f"NOTE_ARRIVED:id={n['id']}:from={n['from_device']}:title={n['title']!r}")
                return 0
        except Exception as e:
            print(f"poll_error:{e}", file=sys.stderr)
        if deadline and time.time() >= deadline:
            print("TIMEOUT")
            return 2
        time.sleep(interval)


def cmd_check(args):
    key = get_key()
    data = api_get("/notes?device=klipper&unread_only=true", key)
    if args.from_dev:
        data = [n for n in data if n.get("from_device") == args.from_dev]
    if args.since:
        data = [n for n in data if n.get("created_at", "") > args.since]
    if args.windows_only:
        data = [n for n in data if n.get("from_device") in WINDOWS_DEVICES]
    if not data:
        print("(no matching unread notes)")
        return 0
    for n in data:
        print(f"#{n['id']}  {n['created_at']}  {n['from_device']:18}  {n['title']}")
    return 0


def main():
    p = argparse.ArgumentParser(prog="handoff.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="upload prompt + write note")
    s.add_argument("--to", required=True, help="target device (surer|windows-masaustu|turer|windows-laptop)")
    s.add_argument("--project", required=True, help="project name (e.g. bilge-english)")
    s.add_argument("--prompt", required=True, help="local path to prompt file")
    s.add_argument("--title", help="note title (default: '<project>: <prompt-stem>')")
    s.add_argument("--task-type", default="gen", help="gen|audit|review|execute (default gen)")
    s.add_argument("--return-dir", help="local dir for return file (default ~/handoffs/<project>)")
    s.add_argument("--output-name", help="explicit output filename (default <project>_<ts>.out)")
    s.add_argument("--target-items", type=int, help="expected item count (used for output token estimate)")
    s.add_argument(
        "--avg-tokens-per-item",
        type=int,
        default=230,
        help="avg output tokens per item (default 230, calibrated on Bilge English A2 batch)",
    )
    s.add_argument("--force", action="store_true", help="send even if estimated output > safe threshold")
    s.set_defaults(func=cmd_send)

    e = sub.add_parser("estimate", help="estimate input/output tokens and recommend batch split")
    e.add_argument("--prompt", required=True)
    e.add_argument("--target-items", type=int, help="expected item count")
    e.add_argument("--avg-tokens-per-item", type=int, default=230)
    e.set_defaults(func=cmd_estimate)

    w = sub.add_parser("wait", help="block until file or new windows note arrives")
    w.add_argument("path", help="expected return file path")
    w.add_argument("--interval", type=int, default=15, help="poll interval seconds (default 15)")
    w.add_argument("--timeout", type=int, default=0, help="give up after N seconds (0 = forever)")
    w.set_defaults(func=cmd_wait)

    c = sub.add_parser("check", help="list unread inbox")
    c.add_argument("--from-dev", help="filter by from_device")
    c.add_argument("--since", help="only created_at > this (e.g. '2026-05-10 13:00')")
    c.add_argument("--windows-only", action="store_true", help="only notes from windows devices")
    c.set_defaults(func=cmd_check)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
