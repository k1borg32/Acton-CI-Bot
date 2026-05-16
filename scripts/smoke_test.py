"""Smoke test: run the pipeline against a Tolk-only repo and confirm the
formatter emits the 'not an Acton project' friendly message instead of a raw
build error. Run with:  python -m scripts.smoke_test
"""

import asyncio
import sys

from bot.config import RunnerConfig
from bot.services.formatter import format_report
from bot.services.runner import run_acton_pipeline
from bot.services.validator import validate_repo


async def main() -> int:
    url = "https://github.com/ton-blockchain/tolk-bench"
    print(f"[smoke] validating {url}")
    repo = await validate_repo(url, max_size_kb=200_000)
    print(f"[smoke] platform={repo.platform} {repo.full_name} size_kb={repo.size_kb}")

    config = RunnerConfig()
    print(f"[smoke] docker_image={config.docker_image}")
    print("[smoke] running pipeline (clone + acton build/test/check)...")
    result = await run_acton_pipeline(repo, config)

    print(f"[smoke] total_duration_s={result.total_duration_s} error={result.error!r}")
    for step in result.steps:
        print(
            f"[smoke]   step={step.step} ok={step.ok} skipped={step.skipped} "
            f"rc={step.return_code} stderr_head={(step.stderr or '')[:120]!r}"
        )

    report = format_report(result)
    with open("scripts/_smoke_report.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print("\n[smoke] formatted report written to scripts/_smoke_report.txt")

    if "Not an Acton project" in report:
        print("\n[smoke] PASS: friendly 'not an Acton project' message rendered")
        return 0
    print("\n[smoke] FAIL: expected friendly message was not rendered")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
