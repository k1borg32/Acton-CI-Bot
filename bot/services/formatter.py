"""
Telegram message formatter.

Converts RunResult into nicely formatted Telegram messages
using HTML parse mode (more reliable than MarkdownV2 for escaping).
"""

from bot.services.parsers import extract_check_findings
from bot.services.runner import RunResult, StepResult

# Max Telegram message length
MAX_MSG_LEN = 4096


def _status_emoji(step: StepResult) -> str:
    if step.skipped:
        return "⏭"
    if step.timed_out:
        return "⏰"
    if step.ok:
        return "✅"
    return "❌"


def _step_label(step_name: str) -> str:
    labels = {
        "build": "Build",
        "test": "Tests",
        "check": "Lint",
        "fmt": "Format",
    }
    return labels.get(step_name, step_name.capitalize())


def _truncate(text: str, max_len: int = 1500) -> str:
    """Truncate long output, keeping the last N chars (tail is more useful)."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return "…" + text[-(max_len - 1) :]


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _not_an_acton_project(result: RunResult) -> bool:
    """True if the build step failed because the repo has no Acton.toml."""
    for step in result.steps:
        if step.step != "build" or step.skipped or step.ok:
            continue
        haystack = (step.stderr or "") + (step.stdout or "")
        if "Acton.toml not found" in haystack:
            return True
    return False


def _format_not_acton_project(result: RunResult) -> str:
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━",
        "🔬 <b>Acton CI Report</b>",
        f"📦 <code>{_escape_html(result.repo.full_name)}</code>",
        "",
        "⚠️ <b>Not an Acton project</b>",
        "",
        "The repository has no <code>Acton.toml</code> at its root, so "
        "<code>acton build</code> can't build it.",
        "",
        "This bot runs the <b>Acton CLI</b> — the TON contract toolkit for "
        "Tolk projects scaffolded with <code>acton init</code>/"
        "<code>acton new</code>.",
        "",
        "Plain FunC/Tolk repos without an <code>Acton.toml</code> aren't "
        "supported yet.",
        "",
        f"⏱ Total: {result.total_duration_s}s",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def format_report(result: RunResult) -> str:
    """
    Format a full CI report for Telegram (HTML parse mode).

    Example output:
    ─────────────────────
    🔬 Acton CI Report
    📦 owner/repo

    ✅ Build — 2.1s
    ✅ Tests — 5.3s
    ⚠️ Lint — 0.8s
      • warning details...

    ⏱ Total: 8.2s
    ─────────────────────
    """
    lines: list[str] = []

    # Header
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔬 <b>Acton CI Report</b>")
    lines.append(f"📦 <code>{_escape_html(result.repo.full_name)}</code>")
    lines.append("")

    # Not an Acton project — friendlier than dumping the raw build error
    if _not_an_acton_project(result):
        return _format_not_acton_project(result)

    # Clone error
    if result.error:
        lines.append("❌ <b>Error:</b>")
        lines.append(f"<pre>{_escape_html(_truncate(result.error, 800))}</pre>")
        lines.append("")
        lines.append(f"⏱ Total: {result.total_duration_s}s")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    # Step results
    for step in result.steps:
        emoji = _status_emoji(step)
        label = _step_label(step.step)

        if step.skipped:
            lines.append(f"{emoji} <b>{label}</b> — <i>skipped</i>")
            continue

        if step.timed_out:
            lines.append(
                f"{emoji} <b>{label}</b> — <i>timed out after {step.duration_s}s</i>"
            )
            continue

        line = f"{emoji} <b>{label}</b> — {step.duration_s}s"
        if step.summary:
            line += f" · <i>{_escape_html(step.summary)}</i>"
        lines.append(line)

        # Show error details for failed steps
        if not step.ok:
            # For `check`, prefer rendering per-finding JSON diagnostics over
            # dumping the raw stderr. Falls back to stderr if no JSON found.
            if step.step == "check":
                findings = extract_check_findings(step.stdout)
                if findings:
                    for f in findings:
                        loc = f["file"] + (f":{f['line']}" if f["line"] else "")
                        sev = f["severity"].lower()
                        sev_icon = "❌" if sev in ("error", "fatal") else "⚠️"
                        head = f"  {sev_icon} <code>{_escape_html(f['code'] or sev or 'lint')}</code>"
                        if loc:
                            head += f" <code>{_escape_html(loc)}</code>"
                        lines.append(head)
                        if f["message"]:
                            lines.append(f"      {_escape_html(f['message'])}")
                    continue  # skip the generic <pre> dump below
            error_text = step.stderr or step.stdout
            if error_text:
                truncated = _truncate(error_text, 600)
                lines.append(f"<pre>{_escape_html(truncated)}</pre>")

    lines.append("")

    # Summary
    if result.success:
        lines.append("🎉 <b>All checks passed!</b>")
    else:
        timed = [s for s in result.steps if s.timed_out]
        failed = [s for s in result.steps if not s.ok and not s.skipped and not s.timed_out]
        parts = []
        if failed:
            parts.append(
                "⚠️ <b>Failed:</b> " + ", ".join(_step_label(s.step) for s in failed)
            )
        if timed:
            parts.append(
                "⏰ <b>Timed out:</b> " + ", ".join(_step_label(s.step) for s in timed)
            )
        lines.append("\n".join(parts))

    lines.append(f"⏱ Total: {result.total_duration_s}s")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")

    msg = "\n".join(lines)

    # Ensure we don't exceed Telegram's limit
    if len(msg) > MAX_MSG_LEN:
        msg = msg[: MAX_MSG_LEN - 20] + "\n\n<i>…truncated</i>"

    return msg


def format_gas_diff(deltas: list, max_rows: int = 8) -> str:
    """Render a gas-diff section. `deltas` is a list of GasDelta from
    bot.services.gas_diff. Returns "" if there's nothing significant.

    Example output:
      ⛽ <b>Gas changes vs base</b>
        🔻 IncreaseCounter: 1412 → 1185 (-227, -16.1%)
        🔺 DecreaseCounter: 1294 → 1380 (+86, +6.6%)
        🆕 NewMessage: 920 (new)
    """
    if not deltas:
        return ""
    lines = ["⛽ <b>Gas changes vs base</b>"]
    for d in deltas[:max_rows]:
        name = _escape_html(d.name)
        if d.base_avg is None:
            lines.append(f"  🆕 <code>{name}</code>: {d.head_avg} <i>(new)</i>")
        elif d.head_avg is None:
            lines.append(f"  🗑 <code>{name}</code>: was {d.base_avg} <i>(removed)</i>")
        else:
            sign = "+" if d.delta_abs >= 0 else ""
            arrow = "🔺" if d.delta_abs > 0 else "🔻" if d.delta_abs < 0 else "▪️"
            lines.append(
                f"  {arrow} <code>{name}</code>: {d.base_avg} → {d.head_avg} "
                f"({sign}{d.delta_abs}, {sign}{d.delta_pct:.1f}%)"
            )
    if len(deltas) > max_rows:
        lines.append(f"  …+{len(deltas) - max_rows} more")
    return "\n".join(lines)


def format_webhook_header(job) -> str:  # job: WebhookJob, kept untyped to avoid circular import
    """Header prepended to the standard report when the run was triggered by
    a GitHub webhook (PR opened/synchronize/reopened)."""
    safe_title = _escape_html(job.pr_title)[:120]
    safe_author = _escape_html(job.pr_author)
    safe_repo = _escape_html(job.repo.full_name)
    return (
        f"🔔 <b>PR #{job.pr_number}</b> · <a href=\"{job.pr_url}\">{safe_title}</a>\n"
        f"📦 <code>{safe_repo}</code> · author: <b>{safe_author}</b> · "
        f"<code>{job.ref[:7]}</code>"
    )


def format_queue_position(position: int) -> str:
    """Format a queue position message."""
    if position == 0:
        return "⏳ <b>Running your check…</b>"
    return (
        f"📋 Your job is queued at position <b>#{position}</b>.\n"
        f"⏳ Hang tight — I'll post the report when it's ready."
    )


def format_start_message() -> str:
    """Format the /start welcome message."""
    return (
        "👋 <b>Hi! I'm the Acton CI-Bot</b>\n\n"
        "I run CI checks for TON smart contracts directly from "
        "GitHub, GitLab, or Bitbucket repositories.\n\n"
        "<b>What I run:</b>\n"
        "🔨 <code>acton build</code> — compile the contract\n"
        "🧪 <code>acton test</code> — run the test suite\n"
        "🔍 <code>acton check</code> — lint Tolk sources\n"
        "✨ <code>acton fmt --check</code> — verify formatting\n\n"
        "<b>How to use me:</b>\n"
        "Send the command:\n"
        "<code>/check https://github.com/owner/repo</code>\n\n"
        "Or just paste the repo URL.\n\n"
        "<b>Free-tier limits:</b>\n"
        "• 5 checks per hour\n"
        "• 1 active check at a time\n"
        "• Max repo size: 50 MB\n"
        "• Public repositories only\n\n"
        "📚 /help — full reference"
    )


def format_help_message() -> str:
    """Format the /help message."""
    return (
        "📚 <b>Acton CI-Bot help</b>\n\n"
        "<b>Commands:</b>\n"
        "/check <code>&lt;url&gt;</code> — run a check on a repository\n"
        "/check <code>owner/repo @branch</code> — check a specific branch\n"
        "/check <code>owner/repo #abc1234</code> — check a specific commit\n"
        "/retry — re-run the last /check in this chat\n"
        "/start — welcome message\n"
        "/help — this help\n"
        "/status — current queue state\n"
        "/subscribe <code>owner/repo</code> — auto-check this repo's PRs in this chat (admin)\n"
        "/unsubscribe <code>owner/repo</code> — stop auto-checks (admin)\n"
        "/subscriptions — list this chat's subscriptions\n\n"
        "<b>Supported hosts:</b>\n"
        "• GitHub — <code>https://github.com/owner/repo</code>\n"
        "• GitLab — <code>https://gitlab.com/owner/repo</code>\n"
        "• Bitbucket — <code>https://bitbucket.org/owner/repo</code>\n\n"
        "<b>What gets checked:</b>\n"
        "1. 🔨 <b>Build</b> — compile Tolk contracts\n"
        "2. 🧪 <b>Test</b> — run the test suite (if any)\n"
        "3. 🔍 <b>Lint</b> — static analysis\n"
        "4. ✨ <b>Format</b> — <code>acton fmt --check</code>\n\n"
        "The repo must be an Acton project — i.e. have <code>Acton.toml</code> at "
        "the root (created via <code>acton new</code>/<code>acton init</code>).\n\n"
        "<b>Sandboxing:</b>\n"
        "• Each check runs in an isolated, ephemeral Docker container\n"
        "• No network access during the run\n"
        "• Workspace auto-cleaned afterward\n"
    )
