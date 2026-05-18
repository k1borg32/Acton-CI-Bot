"""
/start and /help command handlers.
"""

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import DonationsConfig
from bot.services.formatter import format_start_message, format_help_message
from bot.services.menus import main_reply_keyboard


def setup_common_handler(donations: DonationsConfig) -> Router:
    router = Router(name="common")
    show_donate = bool(donations.ton_address)

    @router.message(CommandStart())
    async def handle_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        # Single message: welcome text + persistent reply keyboard.
        # No duplicate inline keyboard above — same actions, twice = clutter.
        await message.answer(
            format_start_message(),
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(show_donate=show_donate),
            disable_web_page_preview=True,
        )

    @router.message(Command("help"))
    async def handle_help(message: Message) -> None:
        await message.answer(
            format_help_message(),
            parse_mode="HTML",
            reply_markup=main_reply_keyboard(show_donate=show_donate),
            disable_web_page_preview=True,
        )

    return router
