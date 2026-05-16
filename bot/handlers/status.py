"""
/status command — shows queue state.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.queue import JobQueue

router = Router(name="status")


def setup_status_handler(queue: JobQueue) -> Router:

    @router.message(Command("status"))
    async def handle_status(message: Message) -> None:
        active = queue.active_jobs
        pending = queue.pending_jobs
        await message.reply(
            f"📊 <b>Queue status</b>\n\n"
            f"🔄 Active jobs: <b>{active}</b>\n"
            f"📋 Queued: <b>{pending}</b>",
            parse_mode="HTML",
        )

    return router
