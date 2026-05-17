"""
Per-step parsers that pull a one-line human-readable summary out of
Acton CLI output. Each parser is defensive — if the format changes or
output is unexpected, it returns None and the formatter falls back to
rendering just the duration.
"""

from __future__ import annotations

import json
import re

# acton build:
#    Compiling contracts
#    Compiling Counter
#     Finished in 73ms
_BUILD_COMPILING = re.compile(r"^\s*Compiling\s+(\S+)\s*$", re.MULTILINE)

# acton test:
#  ✓ 8 passed in 1 file
#  ✗ 1 failed, 7 passed in 1 file
_TEST_SUMMARY_PASS = re.compile(r"(\d+)\s+passed(?:\s+in\s+(\d+)\s+files?)?")
_TEST_SUMMARY_FAIL = re.compile(r"(\d+)\s+failed")

# acton check:
#    Checking Counter
#    Checking scripts/deploy.tolk
# (lint warnings/errors show up as `warning:` / `error:` markers)
_CHECK_CHECKING = re.compile(r"^\s*Checking\s+(\S+)\s*$", re.MULTILINE)
_CHECK_WARNINGS = re.compile(r"^\s*warning:", re.MULTILINE | re.IGNORECASE)
_CHECK_ERRORS = re.compile(r"^\s*error:", re.MULTILINE | re.IGNORECASE)

# acton fmt --check:
#  All files are properly formatted
#  Error: Files are not formatted     (plus optionally a list of bad files)
_FMT_OK_RE = re.compile(r"All files are properly formatted", re.IGNORECASE)


def _combine(stdout: str, stderr: str) -> str:
    return f"{stdout or ''}\n{stderr or ''}"


def summarize_build(stdout: str, stderr: str, ok: bool) -> str | None:
    text = _combine(stdout, stderr)
    contracts = [
        m.group(1)
        for m in _BUILD_COMPILING.finditer(text)
        # The first "Compiling contracts" is the section banner, not a contract.
        if m.group(1).lower() != "contracts"
    ]
    if ok and contracts:
        n = len(contracts)
        # Show first 3 names + "+N more" if many
        if n <= 3:
            return f"compiled {n} contract{'s' if n != 1 else ''}: {', '.join(contracts)}"
        return f"compiled {n} contracts: {', '.join(contracts[:3])}, +{n - 3} more"
    return None


def summarize_test(stdout: str, stderr: str, ok: bool) -> str | None:
    text = _combine(stdout, stderr)
    failed_match = _TEST_SUMMARY_FAIL.search(text)
    passed_match = _TEST_SUMMARY_PASS.search(text)
    if passed_match is None and failed_match is None:
        return None
    passed = int(passed_match.group(1)) if passed_match else 0
    failed = int(failed_match.group(1)) if failed_match else 0
    files = passed_match.group(2) if (passed_match and passed_match.group(2)) else None
    parts = []
    if failed:
        parts.append(f"{failed} failed")
    if passed:
        parts.append(f"{passed} passed")
    if not parts:
        return None
    suffix = f" in {files} file{'s' if files and files != '1' else ''}" if files else ""
    return ", ".join(parts) + suffix


def _parse_check_json(stdout: str) -> dict | None:
    """Try to extract the JSON envelope from acton check --output-format json.
    Returns None if stdout isn't (or doesn't contain) valid JSON.
    """
    text = stdout.strip()
    if not text:
        return None
    # Acton check writes a clean JSON object to stdout in JSON mode; try the
    # whole thing first, then fall back to the last `{...}` block in case
    # there's a prefix/log line.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def summarize_check(stdout: str, stderr: str, ok: bool) -> str | None:
    """Summarize `acton check --output-format json` output.

    Falls back to the plain-text counters if JSON isn't present (e.g. an
    older Acton version or unexpected output).
    """
    # 1) Preferred path: parse JSON diagnostics
    parsed = _parse_check_json(stdout)
    if parsed is not None:
        diagnostics = parsed.get("diagnostics") or []
        if isinstance(diagnostics, list):
            n_err = sum(1 for d in diagnostics if isinstance(d, dict)
                        and str(d.get("severity", "")).lower() in ("error", "fatal"))
            n_warn = sum(1 for d in diagnostics if isinstance(d, dict)
                         and str(d.get("severity", "")).lower() in ("warning", "warn"))
            n_other = max(0, len(diagnostics) - n_err - n_warn)
            if not diagnostics:
                return "no issues"
            parts = []
            if n_err:
                parts.append(f"{n_err} error{'s' if n_err != 1 else ''}")
            if n_warn:
                parts.append(f"{n_warn} warning{'s' if n_warn != 1 else ''}")
            if n_other:
                parts.append(f"{n_other} other")
            return ", ".join(parts) if parts else f"{len(diagnostics)} findings"

    # 2) Fallback: regex counters on plain text
    text = _combine(stdout, stderr)
    n_checked = len(_CHECK_CHECKING.findall(text))
    n_warnings = len(_CHECK_WARNINGS.findall(text))
    n_errors = len(_CHECK_ERRORS.findall(text))
    if ok and n_checked:
        if n_warnings:
            return f"{n_checked} sources, {n_warnings} warning{'s' if n_warnings != 1 else ''}"
        return f"{n_checked} sources, no issues"
    if not ok and (n_errors or n_warnings):
        parts = []
        if n_errors:
            parts.append(f"{n_errors} error{'s' if n_errors != 1 else ''}")
        if n_warnings:
            parts.append(f"{n_warnings} warning{'s' if n_warnings != 1 else ''}")
        return ", ".join(parts)
    return None


def extract_check_findings(stdout: str, max_findings: int = 5) -> list[dict]:
    """Return up to N diagnostic dicts for richer rendering (formatter
    surfaces these under a failed Lint step). Each dict has best-effort keys:
    `code`, `severity`, `file`, `line`, `message`.
    """
    parsed = _parse_check_json(stdout)
    if parsed is None:
        return []
    diagnostics = parsed.get("diagnostics") or []
    if not isinstance(diagnostics, list):
        return []
    out: list[dict] = []
    for d in diagnostics[:max_findings]:
        if not isinstance(d, dict):
            continue
        # Acton's exact field names may vary slightly across versions; pick
        # the first match from each candidate group.
        code = d.get("code") or d.get("rule") or d.get("id") or ""
        severity = d.get("severity") or d.get("level") or ""
        file_ = d.get("file") or d.get("path") or ""
        # Line might be top-level or nested in `location`/`range`/`span`
        line: int | str = d.get("line") or ""
        if not line:
            loc = d.get("location") or d.get("range") or d.get("span") or {}
            if isinstance(loc, dict):
                line = loc.get("line") or loc.get("start_line") or ""
                if not line:
                    start = loc.get("start") or {}
                    if isinstance(start, dict):
                        line = start.get("line") or ""
        message = d.get("message") or d.get("text") or d.get("description") or ""
        out.append({
            "code": str(code),
            "severity": str(severity),
            "file": str(file_),
            "line": str(line),
            "message": str(message)[:200],
        })
    return out


def summarize_fmt(stdout: str, stderr: str, ok: bool) -> str | None:
    text = _combine(stdout, stderr)
    if ok and _FMT_OK_RE.search(text):
        return "all files properly formatted"
    return None


_SUMMARIZERS = {
    "build": summarize_build,
    "test":  summarize_test,
    "check": summarize_check,
    "fmt":   summarize_fmt,
}


def summarize(step: str, stdout: str, stderr: str, ok: bool) -> str | None:
    fn = _SUMMARIZERS.get(step)
    return fn(stdout, stderr, ok) if fn else None
