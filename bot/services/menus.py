"""
Inline-keyboard factories.

All callback_data strings follow `prefix:action[:arg1[:arg2…]]` so the
router can dispatch with simple F.data.startswith() filters. Keep them
short — Telegram caps callback_data at 64 bytes.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


# Callback-data prefixes (terse to stay under the 64-byte cap)
CB_MENU = "m"           # m:<screen>
CB_CHECK = "c"          # c:start | c:default
CB_TOOLS = "t"          # t:start | t:disasm | t:wrapper | t:verify
CB_SUBS = "s"           # s:list | s:rm:<repo>
CB_REPORT = "r"         # r:retry:<url>[@ref]  | r:url:<encoded>
CB_CANCEL = "x"         # x — clear FSM state


def main_menu() -> InlineKeyboardMarkup:
    """Root menu shown on /start and /menu."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔬 Check a repo", callback_data=f"{CB_CHECK}:start"),
            InlineKeyboardButton(text="🔁 Retry last", callback_data=f"{CB_REPORT}:retry:_"),
        ],
        [
            InlineKeyboardButton(text="🛠 Tools", callback_data=f"{CB_TOOLS}:start"),
            InlineKeyboardButton(text="📋 Subscriptions", callback_data=f"{CB_SUBS}:list"),
        ],
        [
            InlineKeyboardButton(text="📊 Status", callback_data=f"{CB_MENU}:status"),
            InlineKeyboardButton(text="❓ Help", callback_data=f"{CB_MENU}:help"),
        ],
    ])


def tools_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Disasm BoC / address", callback_data=f"{CB_TOOLS}:disasm")],
        [InlineKeyboardButton(text="🛠 Generate wrapper", callback_data=f"{CB_TOOLS}:wrapper")],
        [InlineKeyboardButton(text="🔐 Verify on-chain code", callback_data=f"{CB_TOOLS}:verify")],
        [InlineKeyboardButton(text="« Back", callback_data=f"{CB_MENU}:root")],
    ])


def cancel_only() -> InlineKeyboardMarkup:
    """Single-button keyboard shown during FSM prompts."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data=CB_CANCEL)],
    ])


def check_ref_prompt() -> InlineKeyboardMarkup:
    """Buttons shown when prompting for ref in the /check FSM."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Default branch", callback_data=f"{CB_CHECK}:default")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data=CB_CANCEL)],
    ])


def subscriptions_list(repos: list[str]) -> InlineKeyboardMarkup:
    """Subscription list with an unsubscribe button per row + an Add button."""
    rows = []
    for r in repos:
        # Keep callback_data short — use the repo string itself, capped
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {r}",
                callback_data=f"{CB_SUBS}:rm:{r}"[:64],
            )
        ])
    rows.append([InlineKeyboardButton(text="« Back to menu", callback_data=f"{CB_MENU}:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_actions(
    *,
    retry_url: str | None = None,
    retry_ref: str | None = None,
    pr_url: str | None = None,
    repo_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Inline keyboard attached to a report message. Returns None if
    nothing useful to show."""
    row: list[InlineKeyboardButton] = []
    if retry_url:
        # Compose terse retry callback. Most repos easily fit in 64 bytes
        # after the prefix; we cap to be safe.
        data = f"{CB_REPORT}:retry:{retry_url}"
        if retry_ref:
            data = f"{data}@{retry_ref}"
        if len(data.encode("utf-8")) <= 64:
            row.append(InlineKeyboardButton(text="🔁 Retry", callback_data=data))
    if pr_url:
        row.append(InlineKeyboardButton(text="📋 Open PR", url=pr_url))
    elif repo_url:
        row.append(InlineKeyboardButton(text="📦 Repo on GitHub", url=repo_url))
    if not row:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[row])
