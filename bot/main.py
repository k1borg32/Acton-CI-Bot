"""
Acton CI-Bot — entry point.

Runs two long-lived loops concurrently:
  - aiogram long-polling for Telegram commands
  - aiohttp HTTP server for GitHub webhooks and /healthz
"""

import asyncio
import io
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from aiohttp import web

from bot.config import AppConfig
from bot.handlers.admin import setup_admin_handler
from bot.handlers.check import setup_check_handler
from bot.handlers.common import router as common_router
from bot.handlers.status import setup_status_handler
from bot.handlers.subscriptions import setup_subscriptions_handler
from bot.services.queue import JobQueue
from bot.services.stats import Stats
from bot.services.subscriptions import SubscriptionStore
from bot.services.webhook import make_webhook_app


_utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(_utf8_stdout)],
)
logger = logging.getLogger(__name__)


async def _run_polling(dp: Dispatcher, bot: Bot) -> None:
    logger.info("Bot is ready, starting polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


async def _run_http(app: web.Application, host: str, port: int) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("HTTP server listening on %s:%d", host, port)
    try:
        # Block forever — site.start() returns immediately, we keep this
        # task alive so asyncio.gather doesn't unwind the server.
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    logger.info("Starting Acton CI-Bot...")
    config = AppConfig()

    bot = Bot(
        token=config.bot.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    queue = JobQueue(config.rate_limit)
    subscriptions = SubscriptionStore(config.db_path)
    stats = Stats()

    dp = Dispatcher()
    dp.include_router(common_router)
    dp.include_router(setup_check_handler(config, queue, stats))
    dp.include_router(setup_status_handler(queue))
    dp.include_router(setup_subscriptions_handler(subscriptions, config.bot.admin_ids))
    dp.include_router(setup_admin_handler(
        stats=stats, queue=queue, subscriptions=subscriptions,
        admin_ids=config.bot.admin_ids,
    ))

    await bot.set_my_commands([
        BotCommand(command="start", description="Welcome message"),
        BotCommand(command="check", description="Run CI on a repository"),
        BotCommand(command="retry", description="Re-run the last check in this chat"),
        BotCommand(command="status", description="Queue status"),
        BotCommand(command="subscribe", description="Auto-check a repo's PRs in this chat"),
        BotCommand(command="unsubscribe", description="Stop auto-checks in this chat"),
        BotCommand(command="subscriptions", description="List this chat's subscriptions"),
        BotCommand(command="help", description="Full reference"),
    ])

    http_app = make_webhook_app(
        bot=bot,
        config=config,
        subscriptions=subscriptions,
        queue=queue,
        stats=stats,
        secret=config.webhook.secret,
    )
    if not config.webhook.secret:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is empty — /webhooks/github will reject "
            "every request with 500. Set it in env to enable webhooks."
        )

    await asyncio.gather(
        _run_polling(dp, bot),
        _run_http(http_app, config.webhook.host, config.webhook.port),
    )


if __name__ == "__main__":
    asyncio.run(main())
