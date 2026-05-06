#!/usr/bin/env python3
"""Daily ops digest CLI — thin wrapper over app.core.digest.

Usage:
  python3 automation/digest.py                # plain text to stdout
  python3 automation/digest.py --html         # Telegram-safe HTML
  python3 automation/digest.py --json         # raw JSON
  python3 automation/digest.py --send         # POST HTML to Telegram
  python3 automation/digest.py --force        # bypass NOTHING_NEW guard

Stays silent ("NOTHING_NEW", exit 0, no Telegram POST) when no new bugs,
unread notes, recent commits, pentest findings, or service degradation
are present in the 24h window.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# /opt/linux-ai-server'ı sys.path'e ekle ki "app.core.digest" import çalışsın.
# CLI olarak doğrudan çağrıldığımızda (`python3 automation/digest.py`) cwd
# bağımsız çalışmalı; venv'siz sistem python'u da bu dosyayı alabilir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.digest import (  # noqa: E402
    gather,
    has_signal,
    load_env,
    render_html,
    render_text,
    send_telegram,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--html", action="store_true", help="render HTML to stdout instead of plain text")
    p.add_argument("--json", action="store_true", help="dump raw gathered data as JSON")
    p.add_argument("--send", action="store_true", help="render HTML and POST to Telegram")
    p.add_argument("--force", action="store_true", help="ignore NOTHING_NEW guard")
    args = p.parse_args()

    env = load_env()
    data = gather(token=env.get("GITHUB_TOKEN") or None)

    if args.json:
        print(json.dumps(data, default=str, indent=2))
        return 0

    if not has_signal(data) and not args.force:
        print("NOTHING_NEW")
        return 0

    if args.send:
        ok = send_telegram(render_html(data), env)
        return 0 if ok else 1

    print(render_html(data) if args.html else render_text(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
