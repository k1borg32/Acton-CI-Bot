"""
Inline-menu + FSM handlers.

Surfaces:
  /menu              — main inline menu
  callback queries   — every button defined in bot.services.menus
  FSM flows:
    CheckFSM         — /check via buttons (prompt for repo, then optional ref)
    ToolsFSM         — /disasm /wrapper /verify via prompts

The existing slash commands keep working untouched; this is additive.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import AppConfig
from bot.services.formatter import format_help_message, format_start_message
from bot.services.menus import (
    CB_CANCEL,
    CB_CHECK,
    CB_MENU,
    CB_REPORT,
    CB_SUBS,
    CB_TOOLS,
    cancel_only,
    check_ref_prompt,
    main_menu,
    subscriptions_list,
    tools_menu,
)
from bot.services.queue import JobQueue, RateLimitExceeded
from bot.services.runner import run_acton_pipeline
from bot.services.stats import Stats
from bot.services.subscriptions import SubscriptionStore
from bot.services.validator import validate_repo, ValidationError
from bot.services.menus import report_actions
from bot.services.formatter import format_queue_position, format_report

logger = logging.getLogger(__name__)


class CheckFSM(StatesGroup):
    waiting_for_repo = State()
    waiting_for_ref = State()


class ToolsFSM(StatesGroup):
    disasm_waiting = State()      # waiting for hex/base64 or "address <addr>"
    wrapper_repo = State()         # owner/repo
    wrapper_contract = State()     # contract name (then --ts? could add later)
    verify_repo = State()
    verify_contract = State()
    verify_address = State()


def setup_menu_handler(
    *,
    config: AppConfig,
    queue: JobQueue,
    stats: Stats,
    subscriptions: SubscriptionStore,
    admin_ids: list[int],
    last_check_state: dict[int, "object"],  # shared with handlers/check.py
) -> Router:
    """`last_check_state` is the same in-memory `chat_id → _ParsedArg` dict
    used by the /retry slash handler. We mutate it so menu-driven runs
    are retry-able too."""

    router = Router(name="menu")

    # We need access to the same `_run_and_report` helper as /check, but
    # that's defined inside setup_check_handler. To avoid duplication we
    # inline a slimmer version here.
    from bot.handlers.check import _ParsedArg  # type: ignore

    async def _run(message: Message, url: str, ref: str | None) -> None:
        try:
            repo_info = await validate_repo(url, config.runner.max_repo_size_kb)
        except ValidationError as e:
            await message.answer(e.user_message, parse_mode="HTML")
            return

        last_check_state[message.chat.id] = _ParsedArg(url=url, ref=ref)

        user_id = message.from_user.id if message.from_user else 0
        try:
            position = await queue.acquire(user_id)
        except RateLimitExceeded as e:
            await message.answer(e.user_message, parse_mode="HTML")
            return

        status_msg = await message.answer(
            format_queue_position(position), parse_mode="HTML",
        )
        try:
            result = await run_acton_pipeline(repo_info, config.runner, ref=ref)
            stats.record_check(source="manual", success=result.success)
            report = format_report(result)
            kb = report_actions(
                retry_url=url, retry_ref=ref,
                repo_url=repo_info.url,
            )
            await message.answer(report, parse_mode="HTML", reply_markup=kb)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as e:
            logger.exception("menu /check error")
            stats.record_error("menu_check", e)
            stats.record_check(source="manual", success=False)
            await message.answer(
                "💥 Internal error. Please try again later.",
                parse_mode="HTML",
            )
        finally:
            queue.release(user_id)

    # ─────────────────── /menu ───────────────────

    @router.message(Command("menu"))
    async def cmd_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            format_start_message(),
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    # ─────────────────── main-menu callbacks ───────────────────

    @router.callback_query(F.data == f"{CB_MENU}:root")
    async def cb_menu_root(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.message.edit_text(
            format_start_message(), parse_mode="HTML", reply_markup=main_menu(),
        )
        await call.answer()

    @router.callback_query(F.data == f"{CB_MENU}:status")
    async def cb_menu_status(call: CallbackQuery) -> None:
        await call.message.answer(
            f"📊 <b>Queue status</b>\n\n"
            f"🔄 Active jobs: <b>{queue.active_jobs}</b>\n"
            f"📋 Queued: <b>{queue.pending_jobs}</b>",
            parse_mode="HTML",
        )
        await call.answer()

    @router.callback_query(F.data == f"{CB_MENU}:help")
    async def cb_menu_help(call: CallbackQuery) -> None:
        await call.message.answer(format_help_message(), parse_mode="HTML")
        await call.answer()

    @router.callback_query(F.data == CB_CANCEL)
    async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.message.answer(
            "❌ Cancelled.", parse_mode="HTML", reply_markup=main_menu(),
        )
        await call.answer()

    # ─────────────────── /check via FSM ───────────────────

    @router.callback_query(F.data == f"{CB_CHECK}:start")
    async def cb_check_start(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(CheckFSM.waiting_for_repo)
        await call.message.answer(
            "📥 Send the repo to check.\n\n"
            "Examples:\n"
            "<code>owner/repo</code>\n"
            "<code>https://github.com/owner/repo</code>",
            parse_mode="HTML",
            reply_markup=cancel_only(),
        )
        await call.answer()

    @router.message(CheckFSM.waiting_for_repo)
    async def fsm_check_repo(message: Message, state: FSMContext) -> None:
        from bot.handlers.check import _parse_arg
        parsed = _parse_arg(message.text or "")
        if parsed is None:
            await message.answer(
                "❌ Couldn't parse that. Try <code>owner/repo</code> or a full URL.",
                parse_mode="HTML",
                reply_markup=cancel_only(),
            )
            return
        await state.update_data(url=parsed.url, ref=parsed.ref)
        if parsed.ref is not None:
            # User already specified ref via "owner/repo @branch" — skip the prompt
            await state.clear()
            await _run(message, parsed.url, parsed.ref)
            return
        await state.set_state(CheckFSM.waiting_for_ref)
        await message.answer(
            f"📍 Repo: <code>{parsed.url}</code>\n\n"
            "Send a branch / tag / SHA, or use the button below for the default branch.",
            parse_mode="HTML",
            reply_markup=check_ref_prompt(),
        )

    @router.message(CheckFSM.waiting_for_ref)
    async def fsm_check_ref(message: Message, state: FSMContext) -> None:
        ref = (message.text or "").strip()
        if not ref or any(c in ref for c in (" ", ";", "|", "&", "`", "$")):
            await message.answer("❌ Ref looks invalid. Send a clean branch/SHA name.")
            return
        data = await state.get_data()
        await state.clear()
        await _run(message, data["url"], ref)

    @router.callback_query(F.data == f"{CB_CHECK}:default")
    async def cb_check_default_ref(call: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        url = data.get("url")
        if not url:
            await call.answer("Session expired — start over from /menu", show_alert=True)
            return
        await call.answer()
        await _run(call.message, url, None)

    # ─────────────────── retry callback ───────────────────

    @router.callback_query(F.data.startswith(f"{CB_REPORT}:retry:"))
    async def cb_retry(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        payload = call.data[len(f"{CB_REPORT}:retry:") :]
        # `_` means "use last_check_state for this chat"
        if payload == "_":
            last = last_check_state.get(call.message.chat.id)
            if last is None:
                await call.answer(
                    "No previous check in this chat — start one with 🔬 Check a repo.",
                    show_alert=True,
                )
                return
            url, ref = last.url, last.ref
        else:
            ref = None
            if "@" in payload:
                payload, ref = payload.split("@", 1)
            url = payload
        await call.answer("Re-running…")
        await _run(call.message, url, ref)

    # ─────────────────── subscriptions UI ───────────────────

    @router.callback_query(F.data == f"{CB_SUBS}:list")
    async def cb_subs_list(call: CallbackQuery) -> None:
        subs = subscriptions.list_for_chat(call.message.chat.id)
        if not subs:
            await call.message.answer(
                "📋 No subscriptions in this chat.\n\n"
                "Admins can add: <code>/subscribe owner/repo</code>",
                parse_mode="HTML",
                reply_markup=main_menu(),
            )
        else:
            repos = [s.repo_full_name for s in subs]
            await call.message.answer(
                "📋 <b>Subscriptions</b>\n"
                "<i>Tap a row to unsubscribe (admin only).</i>",
                parse_mode="HTML",
                reply_markup=subscriptions_list(repos),
            )
        await call.answer()

    @router.callback_query(F.data.startswith(f"{CB_SUBS}:rm:"))
    async def cb_subs_remove(call: CallbackQuery) -> None:
        if not admin_ids or (call.from_user and call.from_user.id not in admin_ids):
            await call.answer("Admin-only.", show_alert=True)
            return
        repo = call.data[len(f"{CB_SUBS}:rm:") :]
        removed = subscriptions.remove(call.message.chat.id, repo)
        if removed:
            await call.answer(f"Unsubscribed from {repo}")
            # Re-render the list
            subs = subscriptions.list_for_chat(call.message.chat.id)
            repos = [s.repo_full_name for s in subs]
            if repos:
                try:
                    await call.message.edit_reply_markup(reply_markup=subscriptions_list(repos))
                except Exception:
                    pass
            else:
                try:
                    await call.message.edit_text(
                        "📋 No subscriptions left in this chat.",
                        reply_markup=main_menu(),
                    )
                except Exception:
                    pass
        else:
            await call.answer("Was not subscribed.", show_alert=True)

    # ─────────────────── tools menu + FSM ───────────────────

    @router.callback_query(F.data == f"{CB_TOOLS}:start")
    async def cb_tools_start(call: CallbackQuery) -> None:
        await call.message.answer(
            "🛠 <b>Tools</b>\nPick one:",
            parse_mode="HTML",
            reply_markup=tools_menu(),
        )
        await call.answer()

    @router.callback_query(F.data == f"{CB_TOOLS}:disasm")
    async def cb_tools_disasm(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ToolsFSM.disasm_waiting)
        await call.message.answer(
            "🔍 <b>Disassemble</b>\n\n"
            "Send either:\n"
            "• A BoC hex/base64 string\n"
            "• <code>address &lt;ton-address&gt;</code> (optionally <code>testnet</code>)",
            parse_mode="HTML",
            reply_markup=cancel_only(),
        )
        await call.answer()

    @router.message(ToolsFSM.disasm_waiting)
    async def fsm_disasm(message: Message, state: FSMContext) -> None:
        await state.clear()
        # Hand off to the existing /disasm handler by faking the command
        if message.text:
            message.text = f"/disasm {message.text.strip()}"
        # Re-dispatch through the bot's dispatcher? Simpler: import the
        # handler and call it directly.
        from bot.handlers.tools import setup_tools_handler  # noqa
        # Instead of re-dispatching, call run_acton_adhoc again here would
        # duplicate logic — easiest: tell the user to use the slash command,
        # then they can hit /disasm next time. For now, re-dispatch by
        # posting the message as if it were a fresh command:
        # aiogram doesn't have an easy "re-dispatch" — so simpler: just
        # do what handle_disasm does, by setting message.text and
        # invoking the registered handler. But cleanest is calling a
        # shared function. Refactor candidate; for now just point the
        # user at the slash form.
        await message.answer(
            "Run the equivalent slash command:\n"
            f"<code>{message.text}</code>",
            parse_mode="HTML",
        )

    @router.callback_query(F.data == f"{CB_TOOLS}:wrapper")
    async def cb_tools_wrapper(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ToolsFSM.wrapper_repo)
        await call.message.answer(
            "🛠 <b>Wrapper</b>\n\nStep 1/2: send <code>owner/repo</code>.",
            parse_mode="HTML",
            reply_markup=cancel_only(),
        )
        await call.answer()

    @router.message(ToolsFSM.wrapper_repo)
    async def fsm_wrapper_repo(message: Message, state: FSMContext) -> None:
        repo = (message.text or "").strip()
        if "/" not in repo or any(c in repo for c in (";", "|", "&", "`", "$")):
            await message.answer("❌ Bad input. Try <code>owner/repo</code>.", parse_mode="HTML")
            return
        await state.update_data(repo=repo)
        await state.set_state(ToolsFSM.wrapper_contract)
        await message.answer(
            f"Step 2/2: send contract name from <code>{repo}/Acton.toml</code>.",
            parse_mode="HTML",
            reply_markup=cancel_only(),
        )

    @router.message(ToolsFSM.wrapper_contract)
    async def fsm_wrapper_contract(message: Message, state: FSMContext) -> None:
        contract = (message.text or "").strip()
        data = await state.get_data()
        await state.clear()
        await message.answer(
            "Run the equivalent slash command:\n"
            f"<code>/wrapper {data.get('repo')} {contract}</code>",
            parse_mode="HTML",
        )

    @router.callback_query(F.data == f"{CB_TOOLS}:verify")
    async def cb_tools_verify(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ToolsFSM.verify_repo)
        await call.message.answer(
            "🔐 <b>Verify on-chain code</b>\n\nStep 1/3: send <code>owner/repo</code>.",
            parse_mode="HTML",
            reply_markup=cancel_only(),
        )
        await call.answer()

    @router.message(ToolsFSM.verify_repo)
    async def fsm_verify_repo(message: Message, state: FSMContext) -> None:
        repo = (message.text or "").strip()
        if "/" not in repo:
            await message.answer("❌ Bad input. Try <code>owner/repo</code>.", parse_mode="HTML")
            return
        await state.update_data(repo=repo)
        await state.set_state(ToolsFSM.verify_contract)
        await message.answer("Step 2/3: contract name?", reply_markup=cancel_only())

    @router.message(ToolsFSM.verify_contract)
    async def fsm_verify_contract(message: Message, state: FSMContext) -> None:
        contract = (message.text or "").strip()
        await state.update_data(contract=contract)
        await state.set_state(ToolsFSM.verify_address)
        await message.answer("Step 3/3: deployed TON address?", reply_markup=cancel_only())

    @router.message(ToolsFSM.verify_address)
    async def fsm_verify_address(message: Message, state: FSMContext) -> None:
        address = (message.text or "").strip()
        data = await state.get_data()
        await state.clear()
        await message.answer(
            "Run the equivalent slash command:\n"
            f"<code>/verify {data.get('repo')} {data.get('contract')} {address}</code>",
            parse_mode="HTML",
        )

    return router
