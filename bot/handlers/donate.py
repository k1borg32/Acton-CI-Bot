"""
/donate command. Visible only when DONATE_TON_ADDRESS is set.
Tells users where to send TON if they want to support the bot.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import DonationsConfig

router = Router(name="donate")


def _format_donate(cfg: DonationsConfig) -> tuple[str, InlineKeyboardMarkup | None]:
    if not cfg.ton_address:
        return (
            "💚 <b>Donations</b>\n\n"
            "Donations aren't configured by this bot's operator. "
            "Drop a star on the source repo instead:\n"
            "<a href=\"https://github.com/k1borg32/Acton-CI-Bot\">github.com/k1borg32/Acton-CI-Bot</a>"
        ), None

    text = (
        "💚 <b>Support the bot</b>\n\n"
        f"{_escape(cfg.note)}\n\n"
        "<b>TON address:</b>\n"
        f"<code>{_escape(cfg.ton_address)}</code>\n\n"
        "Tap to copy. Any amount welcome — thank you 🙏"
    )
    # Tonkeeper deep-link: opens a wallet preconfigured with the receive address.
    # Works on iOS/Android; on desktop it offers to install Tonkeeper.
    deep_link = f"https://app.tonkeeper.com/transfer/{cfg.ton_address}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Open in Tonkeeper", url=deep_link)],
        [InlineKeyboardButton(
            text="⭐ Star the repo",
            url="https://github.com/k1borg32/Acton-CI-Bot",
        )],
    ])
    return text, kb


def setup_donate_handler(donations: DonationsConfig) -> Router:

    @router.message(Command("donate"))
    async def handle_donate(message: Message) -> None:
        text, kb = _format_donate(donations)
        await message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )

    return router


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
