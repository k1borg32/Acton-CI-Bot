"""
Markdown rendering of a RunResult for GitHub PR comments.

Telegram uses HTML; GitHub uses GFM. Same data, different syntax.
Mirrors what bot.services.formatter.format_report does so the GitHub
comment is "the same report, different format".
"""

from __future__ import annotations

from bot.services.gas_diff import GasDelta
from bot.services.parsers import extract_check_findings
from bot.services.runner import RunResult, StepResult, TIMEOUT_RETURN_CODE


def _emoji(step: StepResult) -> str:
    if step.skipped:
        return "⏭️"
    if step.timed_out:
        return "⏰"
    if step.ok:
        return "✅"
    return "❌"


def _label(step: str) -> str:
    return {"build": "Build", "test": "Tests", "check": "Lint", "fmt": "Format"}.get(
        step, step.capitalize()
    )


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else "…" + text[-(n - 1) :]


def format_pr_comment(
    result: RunResult,
    *,
    head_sha: str,
    pr_url: str,
    gas_deltas: list[GasDelta] | None = None,
) -> str:
    """Render a markdown PR comment. Self-contained — caller doesn't need
    to add a header/footer."""
    lines: list[str] = []
    lines.append(f"### 🔬 Acton CI report · `{head_sha[:7]}`")
    lines.append("")

    if result.error:
        lines.append("❌ **Git clone failed**")
        lines.append("")
        lines.append("```")
        lines.append(_truncate(result.error, 800))
        lines.append("```")
        return "\n".join(lines)

    # Per-step table
    lines.append("| | Step | Duration | Details |")
    lines.append("|---|---|---|---|")
    for s in result.steps:
        emoji = _emoji(s)
        if s.skipped:
            lines.append(f"| {emoji} | **{_label(s.step)}** | – | *skipped* |")
        elif s.timed_out:
            lines.append(
                f"| {emoji} | **{_label(s.step)}** | {s.duration_s}s | "
                f"*timed out* |"
            )
        else:
            detail = s.summary or ""
            lines.append(
                f"| {emoji} | **{_label(s.step)}** | {s.duration_s}s | {detail} |"
            )

    lines.append("")

    # Failed-step diagnostics (especially `check`)
    for s in result.steps:
        if s.skipped or s.ok or s.timed_out:
            continue
        if s.step == "check":
            findings = extract_check_findings(s.stdout)
            if findings:
                lines.append("<details><summary>Lint findings</summary>")
                lines.append("")
                for f in findings:
                    loc = f["file"] + (f":{f['line']}" if f["line"] else "")
                    sev = f["severity"] or "lint"
                    code = f["code"] or sev
                    lines.append(f"- `{code}` `{loc}` — {f['message']}")
                lines.append("")
                lines.append("</details>")
                lines.append("")
                continue
        # Generic: show tail of stderr in a collapsible block
        err = (s.stderr or s.stdout).strip()
        if err:
            lines.append(f"<details><summary>{_label(s.step)} output</summary>")
            lines.append("")
            lines.append("```")
            lines.append(_truncate(err, 1500))
            lines.append("```")
            lines.append("</details>")
            lines.append("")

    # Gas diff section
    if gas_deltas:
        lines.append("### ⛽ Gas changes vs base")
        lines.append("")
        lines.append("| | Opcode | Base | Head | Δ | Δ% |")
        lines.append("|---|---|---|---|---|---|")
        for d in gas_deltas[:12]:
            if d.base_avg is None:
                lines.append(f"| 🆕 | `{d.name}` | – | {d.head_avg} | new | – |")
            elif d.head_avg is None:
                lines.append(f"| 🗑 | `{d.name}` | {d.base_avg} | – | removed | – |")
            else:
                arrow = "🔺" if d.delta_abs > 0 else "🔻" if d.delta_abs < 0 else "▪️"
                sign = "+" if d.delta_abs >= 0 else ""
                lines.append(
                    f"| {arrow} | `{d.name}` | {d.base_avg} | {d.head_avg} | "
                    f"{sign}{d.delta_abs} | {sign}{d.delta_pct:.1f}% |"
                )
        lines.append("")

    # Summary line
    if result.success:
        lines.append("✅ **All checks passed**")
    else:
        timed = [s for s in result.steps if s.timed_out]
        failed = [s for s in result.steps if not s.ok and not s.skipped and not s.timed_out]
        bits = []
        if failed:
            bits.append("**Failed:** " + ", ".join(_label(s.step) for s in failed))
        if timed:
            bits.append("**Timed out:** " + ", ".join(_label(s.step) for s in timed))
        lines.append(" · ".join(bits))

    lines.append("")
    lines.append(f"⏱ Total: **{result.total_duration_s}s**")
    lines.append("")
    lines.append(
        f"<sub>Posted by [Acton CI-Bot](https://github.com/k1borg32/Acton-CI-Bot) · "
        f"<a href=\"{pr_url}\">PR view</a></sub>"
    )
    return "\n".join(lines)
