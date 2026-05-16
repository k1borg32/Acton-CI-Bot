"""
/admin command — operator visibility into runtime state.

Currently:
  /admin stats   — runtime, total check counts, recent errors
"""

from __future__ import annotations

import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.queue import JobQueue
from bot.services.stats import Stats
from bot.services.subscriptions import SubscriptionStore

router = Router(name="admin")


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or parts:
        parts.append(f"{h}h")
    if m or parts:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def setup_admin_handler(
    *,
    stats: Stats,
    queue: JobQueue,
    subscriptions: SubscriptionStore,
    admin_ids: list[int],
) -> Router:

    @router.message(Command("admin"))
    async def handle_admin(message: Message) -> None:
        if message.from_user is None or message.from_user.id not in admin_ids:
            await message.reply("🚫 Admin-only.", parse_mode="HTML")
            return

        # Parse the subcommand: /admin stats
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else "stats"

        if sub != "stats":
            await message.reply(
                "Usage: <code>/admin stats</code>", parse_mode="HTML"
            )
            return

        recent = stats.recent_errors()
        now = time.time()
        recent_lines: list[str] = []
        for ts, msg in recent:
            age_s = int(now - ts)
            recent_lines.append(f"  • <code>-{age_s}s</code> {msg}")
        recent_block = "\n".join(recent_lines) if recent_lines else "  (none)"

        lines = [
            "🛠 <b>Admin stats</b>",
            "",
            f"⏱ Uptime: <code>{_fmt_uptime(stats.uptime_s)}</code>",
            "",
            "<b>Pipeline runs (since restart)</b>",
            f"• total:    <b>{stats.checks_total}</b>",
            f"• manual:   <b>{stats.checks_manual}</b>",
            f"• webhook:  <b>{stats.checks_webhook}</b>",
            f"• success:  <b>{stats.checks_success}</b>",
            f"• failed:   <b>{stats.checks_failed}</b>",
            "",
            "<b>Queue</b>",
            f"• active:  <b>{queue.active_jobs}</b>",
            f"• pending: <b>{queue.pending_jobs}</b>",
            "",
            "<b>Subscriptions</b>",
            f"• total rows: <b>{subscriptions.count()}</b>",
            "",
            "<b>Recent errors</b>",
            recent_block,
        ]
        await message.reply("\n".join(lines), parse_mode="HTML")

    return router
