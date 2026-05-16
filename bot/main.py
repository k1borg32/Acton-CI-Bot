"""
Acton CI-Bot — entry point.

Initializes the bot, registers handlers, and starts polling.
"""

import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from bot.config import AppConfig
from bot.handlers.check import setup_check_handler
from bot.handlers.common import router as common_router
from bot.handlers.status import setup_status_handler
from bot.services.queue import JobQueue

# Logging — force UTF-8 to avoid Windows cp1252 encoding errors
import io

_utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(_utf8_stdout)],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Starting Acton CI-Bot...")

    # Load config from environment
    config = AppConfig()

    # Initialize bot
    bot = Bot(
        token=config.bot.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Initialize queue
    queue = JobQueue(config.rate_limit)

    # Setup dispatcher and handlers
    dp = Dispatcher()
    dp.include_router(common_router)
    dp.include_router(setup_check_handler(config, queue))
    dp.include_router(setup_status_handler(queue))

    # Set bot commands menu
    from aiogram.types import BotCommand

    await bot.set_my_commands([
        BotCommand(command="start", description="Приветствие"),
        BotCommand(command="check", description="Проверить репозиторий"),
        BotCommand(command="status", description="Статус очереди"),
        BotCommand(command="help", description="Справка"),
    ])

    logger.info("Bot is ready, starting polling...")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
