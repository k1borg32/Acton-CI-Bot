"""
/check command handler — core CI functionality.
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import AppConfig
from bot.services.formatter import format_report, format_queue_position
from bot.services.queue import JobQueue, RateLimitExceeded
from bot.services.runner import run_acton_pipeline
from bot.services.stats import Stats
from bot.services.validator import validate_repo, ValidationError

logger = logging.getLogger(__name__)
router = Router(name="check")


def _extract_url(message: Message) -> str | None:
    """Extract URL from /check command or plain message."""
    if message.text is None:
        return None

    text = message.text.strip()

    # /check <url>
    if text.startswith("/check"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else None

    # Plain URL message
    if text.startswith("https://"):
        return text.split()[0]

    return None


def setup_check_handler(config: AppConfig, queue: JobQueue, stats: Stats) -> Router:
    """Register the /check handler with dependencies."""

    @router.message(Command("check"))
    async def handle_check(message: Message) -> None:
        url = _extract_url(message)
        if not url:
            await message.reply(
                "❓ Provide a repository URL:\n"
                "<code>/check https://github.com/owner/repo</code>",
                parse_mode="HTML",
            )
            return

        # Validate URL
        try:
            repo_info = await validate_repo(url, config.runner.max_repo_size_kb)
        except ValidationError as e:
            await message.reply(e.user_message, parse_mode="HTML")
            return

        # Check rate limits
        user_id = message.from_user.id if message.from_user else 0
        try:
            position = await queue.acquire(user_id)
        except RateLimitExceeded as e:
            await message.reply(e.user_message, parse_mode="HTML")
            return

        # Send "processing" message
        status_msg = await message.reply(
            format_queue_position(position),
            parse_mode="HTML",
        )

        try:
            # Run the pipeline
            result = await run_acton_pipeline(repo_info, config.runner)
            stats.record_check(source="manual", success=result.success)

            # Send report
            report = format_report(result)
            await message.reply(report, parse_mode="HTML")

            # Delete status message
            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.exception("Pipeline error for %s", url)
            stats.record_error("check_handler", e)
            stats.record_check(source="manual", success=False)
            await message.reply(
                "💥 Internal error. Please try again later.",
                parse_mode="HTML",
            )
        finally:
            queue.release(user_id)

    # Also handle plain URLs (just https:// links sent as messages)
    @router.message(F.text.startswith("https://github.com/")
                    | F.text.startswith("https://gitlab.com/")
                    | F.text.startswith("https://bitbucket.org/"))
    async def handle_plain_url(message: Message) -> None:
        """Treat plain repo URLs the same as /check."""
        # Reuse the same logic
        url = _extract_url(message)
        if url:
            message.text = f"/check {url}"
            await handle_check(message)

    return router
