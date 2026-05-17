"""
Per-step parsers that pull a one-line human-readable summary out of
Acton CLI output. Each parser is defensive — if the format changes or
output is unexpected, it returns None and the formatter falls back to
rendering just the duration.
"""

from __future__ import annotations

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


def summarize_check(stdout: str, stderr: str, ok: bool) -> str | None:
    text = _combine(stdout, stderr)
    n_checked = len(_CHECK_CHECKING.findall(text))
    n_warnings = len(_CHECK_WARNINGS.findall(text))
    n_errors = len(_CHECK_ERRORS.findall(text))
    if ok and n_checked:
        if n_warnings:
            return f"{n_checked} sources, {n_warnings} warning{'s' if n_warnings != 1 else ''}"
        return f"{n_checked} sources, no issues"
    if not ok:
        if n_errors or n_warnings:
            parts = []
            if n_errors:
                parts.append(f"{n_errors} error{'s' if n_errors != 1 else ''}")
            if n_warnings:
                parts.append(f"{n_warnings} warning{'s' if n_warnings != 1 else ''}")
            return ", ".join(parts)
    return None


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
