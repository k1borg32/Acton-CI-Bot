"""
Telegram message formatter.

Converts RunResult into nicely formatted Telegram messages
using HTML parse mode (more reliable than MarkdownV2 for escaping).
"""

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
        "⚠️ <b>Это не Acton-проект</b>",
        "",
        "В корне репозитория нет <code>Acton.toml</code>, поэтому "
        "<code>acton build</code> не может его собрать.",
        "",
        "Этот бот запускает <b>Acton CLI</b> — тулкит для TON-контрактов "
        "на Tolk, инициализируемый через <code>acton init</code>/"
        "<code>acton new</code>.",
        "",
        "Репозитории на чистом FunC/Tolk без Acton.toml пока не поддерживаются.",
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
        lines.append(f"❌ <b>Error:</b>")
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

        lines.append(f"{emoji} <b>{label}</b> — {step.duration_s}s")

        # Show error details for failed steps
        if not step.ok:
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
        return "⏳ <b>Запускаю проверку…</b>"
    return (
        f"📋 Ваша задача в очереди: позиция <b>#{position}</b>\n"
        f"⏳ Ожидайте, я отправлю отчёт когда всё будет готово."
    )


def format_start_message() -> str:
    """Format the /start welcome message."""
    return (
        "👋 <b>Привет! Я Acton CI-Bot</b>\n\n"
        "Я запускаю автоматические проверки для TON смарт-контрактов "
        "прямо из GitHub, GitLab или Bitbucket.\n\n"
        "<b>Что я делаю:</b>\n"
        "🔨 <code>acton build</code> — компиляция контракта\n"
        "🧪 <code>acton test</code> — запуск тестов\n"
        "🔍 <code>acton check</code> — линтинг Tolk-исходников\n"
        "✨ <code>acton fmt --check</code> — проверка форматирования\n\n"
        "<b>Как пользоваться:</b>\n"
        "Отправь команду:\n"
        "<code>/check https://github.com/owner/repo</code>\n\n"
        "Или просто скинь ссылку на репозиторий.\n\n"
        "<b>Лимиты (бесплатный план):</b>\n"
        f"• 5 проверок в час\n"
        f"• 1 активная проверка одновременно\n"
        f"• Максимальный размер репо: 50 MB\n"
        f"• Только публичные репозитории\n\n"
        "📚 /help — подробная справка"
    )


def format_help_message() -> str:
    """Format the /help message."""
    return (
        "📚 <b>Справка Acton CI-Bot</b>\n\n"
        "<b>Команды:</b>\n"
        "/check <code>&lt;url&gt;</code> — запустить проверку репозитория\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/status — текущий статус очереди\n\n"
        "<b>Поддерживаемые платформы:</b>\n"
        "• GitHub — <code>https://github.com/owner/repo</code>\n"
        "• GitLab — <code>https://gitlab.com/owner/repo</code>\n"
        "• Bitbucket — <code>https://bitbucket.org/owner/repo</code>\n\n"
        "<b>Что проверяется:</b>\n"
        "1. 🔨 <b>Build</b> — компиляция Tolk-контрактов\n"
        "2. 🧪 <b>Test</b> — запуск тестов (если есть)\n"
        "3. 🔍 <b>Check</b> — линтинг и статический анализ\n"
        "4. ✨ <b>Format</b> — <code>acton fmt --check</code>\n\n"
        "Репозиторий должен быть Acton-проектом — с <code>Acton.toml</code> "
        "в корне (создаётся через <code>acton new</code>/<code>acton init</code>).\n\n"
        "<b>Безопасность:</b>\n"
        "• Каждая проверка в изолированном Docker-контейнере\n"
        "• Без доступа к сети во время выполнения\n"
        "• Автоматическая очистка после завершения\n"
    )
