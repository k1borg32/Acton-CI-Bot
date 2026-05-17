"""
/check, /retry command handlers — manual CI runs.

URL arg syntax:
  /check https://github.com/owner/repo                — clone default branch
  /check https://github.com/owner/repo @feature       — clone branch `feature`
  /check https://github.com/owner/repo #abc1234       — clone & checkout SHA
  /check owner/repo                                    — GitHub shorthand
"""

import logging
from dataclasses import dataclass

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import AppConfig
from bot.services.formatter import format_report, format_queue_position
from bot.services.menus import report_actions
from bot.services.queue import JobQueue, RateLimitExceeded
from bot.services.runner import run_acton_pipeline
from bot.services.stats import Stats
from bot.services.validator import validate_repo, ValidationError, RepoInfo

logger = logging.getLogger(__name__)
router = Router(name="check")


@dataclass(frozen=True)
class _ParsedArg:
    url: str
    ref: str | None  # branch / tag / SHA (or None for default branch)


def _parse_arg(text: str) -> _ParsedArg | None:
    """Pull `url` + optional `ref` out of a /check argument.

    Accepted forms (after the command):
      https://github.com/owner/repo
      https://github.com/owner/repo @branch
      https://github.com/owner/repo #abc1234
      owner/repo                   (GitHub shorthand → https://github.com/owner/repo)
      owner/repo @branch
    """
    arg = text.strip()
    if not arg:
        return None

    # Split off optional ref suffix (`@branch` or `#sha`)
    ref: str | None = None
    for sep in (" @", " #"):
        if sep in arg:
            arg, ref_part = arg.split(sep, 1)
            ref = ref_part.strip() or None
            break

    arg = arg.strip()
    if arg.startswith("https://"):
        url = arg
    elif "/" in arg and " " not in arg and arg.count("/") == 1:
        owner, repo = arg.split("/")
        if owner and repo and not any(c in arg for c in (";", "|", "&", "$", "`")):
            url = f"https://github.com/{owner}/{repo}"
        else:
            return None
    else:
        return None
    return _ParsedArg(url=url, ref=ref)


def _extract_check_arg(message: Message) -> str | None:
    """Pull the argument string after /check (or treat a plain URL as the arg)."""
    if message.text is None:
        return None
    text = message.text.strip()
    if text.startswith("/check"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else None
    if text.startswith("https://"):
        return text  # whole message is the URL (+ optional ref)
    return None


def setup_check_handler(
    config: AppConfig,
    queue: JobQueue,
    stats: Stats,
    last_check: dict[int, "_ParsedArg"],
) -> Router:
    """Register the /check, /retry and bare-URL handlers.

    `last_check` is the chat_id → last-/check-args dict, shared with the
    menu handler so button-driven /retry works against menu-initiated checks.
    """

    async def _run_and_report(
        message: Message,
        repo_info: RepoInfo,
        ref: str | None,
    ) -> None:
        user_id = message.from_user.id if message.from_user else 0
        try:
            position = await queue.acquire(user_id)
        except RateLimitExceeded as e:
            await message.reply(e.user_message, parse_mode="HTML")
            return

        status_msg = await message.reply(
            format_queue_position(position),
            parse_mode="HTML",
        )

        try:
            result = await run_acton_pipeline(repo_info, config.runner, ref=ref)
            stats.record_check(source="manual", success=result.success)
            report = format_report(result)
            kb = report_actions(
                retry_url=repo_info.url,
                retry_ref=ref,
                repo_url=repo_info.url,
            )
            await message.reply(report, parse_mode="HTML", reply_markup=kb)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as e:
            logger.exception("Pipeline error for %s ref=%s", repo_info.url, ref)
            stats.record_error("check_handler", e)
            stats.record_check(source="manual", success=False)
            await message.reply(
                "💥 Internal error. Please try again later.",
                parse_mode="HTML",
            )
        finally:
            queue.release(user_id)

    @router.message(Command("check"))
    async def handle_check(message: Message) -> None:
        raw = _extract_check_arg(message)
        if not raw:
            await message.reply(
                "❓ Provide a repository URL:\n"
                "<code>/check https://github.com/owner/repo</code>\n"
                "<code>/check owner/repo</code>\n"
                "<code>/check owner/repo @feature</code>\n"
                "<code>/check owner/repo #abc1234</code>",
                parse_mode="HTML",
            )
            return

        parsed = _parse_arg(raw)
        if parsed is None:
            await message.reply(
                "❌ Couldn't parse that. Try:\n"
                "<code>/check owner/repo</code> or "
                "<code>/check https://github.com/owner/repo @branch</code>",
                parse_mode="HTML",
            )
            return

        try:
            repo_info = await validate_repo(parsed.url, config.runner.max_repo_size_kb)
        except ValidationError as e:
            await message.reply(e.user_message, parse_mode="HTML")
            return

        last_check[message.chat.id] = parsed
        await _run_and_report(message, repo_info, parsed.ref)

    @router.message(Command("retry"))
    async def handle_retry(message: Message) -> None:
        last = last_check.get(message.chat.id)
        if last is None:
            await message.reply(
                "🤷 No previous /check in this chat to retry.\n"
                "Run <code>/check &lt;url&gt;</code> first.",
                parse_mode="HTML",
            )
            return
        try:
            repo_info = await validate_repo(last.url, config.runner.max_repo_size_kb)
        except ValidationError as e:
            await message.reply(e.user_message, parse_mode="HTML")
            return
        ref_note = f" @{last.ref}" if last.ref else ""
        await message.reply(
            f"🔁 Retrying <code>{repo_info.full_name}</code>{ref_note}…",
            parse_mode="HTML",
        )
        await _run_and_report(message, repo_info, last.ref)

    # Plain URL message → behave like /check
    @router.message(F.text.startswith("https://github.com/")
                    | F.text.startswith("https://gitlab.com/")
                    | F.text.startswith("https://bitbucket.org/"))
    async def handle_plain_url(message: Message) -> None:
        if message.text:
            message.text = f"/check {message.text.strip()}"
            await handle_check(message)

    return router
