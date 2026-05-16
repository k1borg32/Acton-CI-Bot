"""
/subscribe, /unsubscribe, /subscriptions command handlers.

Subscribing a repo to a chat enables automatic CI reports for that repo's
GitHub webhook events.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.subscriptions import SubscriptionStore
from bot.services.validator import parse_repo_url, ValidationError

logger = logging.getLogger(__name__)
router = Router(name="subscriptions")


_GITHUB_REPO_HINT = (
    "Provide a repo: <code>/subscribe owner/repo</code>\n"
    "Or a full URL: <code>/subscribe https://github.com/owner/repo</code>"
)


def _parse_repo_arg(text: str | None) -> str | None:
    """Extract `owner/repo` from a /subscribe argument.

    Accepts:
      /subscribe owner/repo
      /subscribe https://github.com/owner/repo
      /subscribe https://github.com/owner/repo.git
    Only GitHub is supported (webhooks only fire from GitHub for now).
    """
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    arg = parts[1].strip()

    if arg.startswith("https://"):
        try:
            info = parse_repo_url(arg)
        except ValidationError:
            return None
        if info.platform != "github":
            return None
        return f"{info.owner}/{info.repo}"

    # Bare "owner/repo" form
    if arg.count("/") == 1 and all(s and not s.startswith("-") for s in arg.split("/")):
        return arg.lower()

    return None


def setup_subscriptions_handler(
    store: SubscriptionStore,
    admin_ids: list[int],
) -> Router:

    def _is_admin(user_id: int) -> bool:
        # If no admins configured, lock down subscription mutation entirely
        return bool(admin_ids) and user_id in admin_ids

    @router.message(Command("subscribe"))
    async def handle_subscribe(message: Message) -> None:
        if message.from_user is None:
            return
        if not _is_admin(message.from_user.id):
            await message.reply(
                "🚫 Subscription management is admin-only.",
                parse_mode="HTML",
            )
            return

        repo = _parse_repo_arg(message.text)
        if not repo:
            await message.reply(_GITHUB_REPO_HINT, parse_mode="HTML")
            return

        chat_id = message.chat.id
        added = store.add(chat_id, repo, message.from_user.id)
        if added:
            logger.info("subscribed chat %s to %s", chat_id, repo)
            await message.reply(
                f"✅ Chat subscribed to <code>{repo}</code>.\n\n"
                "Webhook events (PR opened / synchronize / reopened) will now "
                "trigger an automatic CI run, with the report posted here.",
                parse_mode="HTML",
            )
        else:
            await message.reply(
                f"ℹ️ Chat is already subscribed to <code>{repo}</code>.",
                parse_mode="HTML",
            )

    @router.message(Command("unsubscribe"))
    async def handle_unsubscribe(message: Message) -> None:
        if message.from_user is None:
            return
        if not _is_admin(message.from_user.id):
            await message.reply(
                "🚫 Subscription management is admin-only.",
                parse_mode="HTML",
            )
            return

        repo = _parse_repo_arg(message.text)
        if not repo:
            await message.reply(
                "Provide a repo: <code>/unsubscribe owner/repo</code>",
                parse_mode="HTML",
            )
            return

        removed = store.remove(message.chat.id, repo)
        if removed:
            logger.info("unsubscribed chat %s from %s", message.chat.id, repo)
            await message.reply(
                f"✅ Chat unsubscribed from <code>{repo}</code>.", parse_mode="HTML"
            )
        else:
            await message.reply(
                f"ℹ️ Chat wasn't subscribed to <code>{repo}</code>.",
                parse_mode="HTML",
            )

    @router.message(Command("subscriptions"))
    async def handle_list(message: Message) -> None:
        subs = store.list_for_chat(message.chat.id)
        if not subs:
            await message.reply(
                "📋 No active subscriptions in this chat.\n\n"
                "An admin can add one with: <code>/subscribe owner/repo</code>",
                parse_mode="HTML",
            )
            return

        lines = ["📋 <b>This chat's subscriptions:</b>", ""]
        for s in subs:
            lines.append(f"• <code>{s.repo_full_name}</code>")
        await message.reply("\n".join(lines), parse_mode="HTML")

    return router
