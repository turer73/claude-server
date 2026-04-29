"""CI/CD Telegram notification — direct, n8n-independent."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def notify_ci_result(
    total: int,
    passed: int,
    failed: int,
    projects: list[dict],
    run_id: int | None = None,
    trigger: str = "manual",
) -> None:
    """Send CI run results to Telegram. Fires on every run-all completion."""
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return

    if failed == 0:
        emoji = "\u2705"
        status = "ALL PASSED"
    else:
        emoji = "\u274c"
        status = f"{failed} FAILED"

    lines = [
        f"{emoji} *CI/CD \u2014 {status}*",
        f"Tests: {passed}/{total} passed"
        + (f" | Run #{run_id}" if run_id else "")
        + f" | {trigger}",
        "",
    ]

    # Failed projects detail
    failed_projects = [p for p in projects if p.get("failed", 0) > 0]
    if failed_projects:
        lines.append("*Failed:*")
        for p in failed_projects:
            line = f"  \u2022 `{p['project']}`: {p['failed']} fail / {p['passed']} pass"
            fix = p.get("fix_result", "")
            if fix:
                line += f" ({fix})"
            lines.append(line)
        lines.append("")

    # All projects summary
    lines.append("*All projects:*")
    for p in projects:
        icon = "\u2705" if p.get("failed", 0) == 0 else "\u274c"
        lines.append(f"  {icon} `{p['project']}`: {p.get('passed', 0)}/{p.get('total', 0)}")
    lines.append("")
    lines.append(f"\U0001f551 {datetime.now(timezone.utc).strftime('%H:%M %d/%m/%Y')}")

    text = "\n".join(lines)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as exc:
        logger.warning("Failed to send CI Telegram notification: %s", exc)
