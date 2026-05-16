"""
/start and /help command handlers.
"""

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from bot.services.formatter import format_start_message, format_help_message

router = Router(name="common")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.reply(format_start_message(), parse_mode="HTML")


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.reply(format_help_message(), parse_mode="HTML")
