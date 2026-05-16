"""End-to-end test suite for Acton CI-Bot.

Run with:
    BOT_TOKEN=dummy ACTON_DOCKER_IMAGE=acton-runner:latest \
        python -m scripts.e2e_test

Covers:
  - Validator (unit)
  - JobQueue rate limiting (unit, async)
  - Formatter (unit) — start, help, queue position, success, failure,
    not-an-Acton-project, timeout
  - Runner integration:
      * Image smoke test (`acton --version`)
      * tolk-bench (non-Acton) → friendly message via real git clone
      * acton new counter → build/test/check/fmt all pass (no git clone,
        we drive run_acton_steps directly against a pre-scaffolded dir)
      * Timeout path → ⏰ rendered
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from bot.config import RateLimitConfig, RunnerConfig
from bot.services.formatter import (
    format_help_message,
    format_queue_position,
    format_report,
    format_start_message,
)
from bot.services.queue import JobQueue, RateLimitExceeded
from bot.services.runner import (
    RunResult,
    StepResult,
    TIMEOUT_RETURN_CODE,
    _run_subprocess,
    run_acton_pipeline,
    run_acton_steps,
)
from bot.services.subscriptions import SubscriptionStore
from bot.services.validator import (
    RepoInfo,
    ValidationError,
    parse_repo_url,
)
from bot.services.webhook import _parse_pr_event, _verify_signature
from bot.handlers.subscriptions import _parse_repo_arg


# ───────────────── helpers ─────────────────


@dataclass
class TestCase:
    name: str
    ok: bool
    detail: str = ""


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def assert_eq(actual, expected, msg: str) -> None:
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_contains(haystack: str, needle: str, msg: str) -> None:
    if needle not in haystack:
        raise AssertionError(f"{msg}: {needle!r} not found in {haystack[:200]!r}…")


def report_case(name: str, fn) -> TestCase:
    try:
        fn()
        print(f"  PASS  {name}")
        return TestCase(name=name, ok=True)
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        return TestCase(name=name, ok=False, detail=str(e))
    except Exception as e:
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
        return TestCase(name=name, ok=False, detail=f"{type(e).__name__}: {e}")


async def report_case_async(name: str, coro_fn) -> TestCase:
    try:
        await coro_fn()
        print(f"  PASS  {name}")
        return TestCase(name=name, ok=True)
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        return TestCase(name=name, ok=False, detail=str(e))
    except Exception as e:
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
        return TestCase(name=name, ok=False, detail=f"{type(e).__name__}: {e}")


# ───────────────── validator tests ─────────────────


def t_validator_valid_urls() -> None:
    cases = [
        ("https://github.com/owner/repo", "github", "owner", "repo"),
        ("https://github.com/owner/repo.git", "github", "owner", "repo"),
        ("https://github.com/owner/repo/", "github", "owner", "repo"),
        ("https://gitlab.com/owner/repo", "gitlab", "owner", "repo"),
        ("https://bitbucket.org/owner/repo", "bitbucket", "owner", "repo"),
    ]
    for url, platform, owner, repo in cases:
        info = parse_repo_url(url)
        assert_eq(info.platform, platform, f"{url} platform")
        assert_eq(info.owner, owner, f"{url} owner")
        assert_eq(info.repo, repo, f"{url} repo")


def t_validator_rejects_bad_urls() -> None:
    bad = [
        "file:///etc/passwd",
        "ssh://git@github.com/owner/repo",
        "http://github.com/owner/repo",  # plain http
        "https://evil.com/owner/repo",
        "https://github.com/owner/repo;rm -rf /",
        "not a url at all",
        "",
    ]
    for url in bad:
        try:
            parse_repo_url(url)
        except ValidationError:
            continue
        raise AssertionError(f"validator should reject {url!r}")


# ───────────────── queue tests ─────────────────


async def t_queue_per_user_hourly_limit() -> None:
    cfg = RateLimitConfig()
    q = JobQueue(cfg)
    # Saturate the per-hour quota
    for _ in range(cfg.max_checks_per_hour):
        await q.acquire(user_id=42)
        q.release(user_id=42)
    # Next call should be rate-limited
    try:
        await q.acquire(user_id=42)
    except RateLimitExceeded as e:
        assert_contains(e.user_message, "Лимит", "hourly limit msg")
        return
    raise AssertionError("expected hourly limit to fire")


async def t_queue_per_user_concurrent_limit() -> None:
    q = JobQueue(RateLimitConfig())
    await q.acquire(user_id=7)
    try:
        await q.acquire(user_id=7)  # already has one active
    except RateLimitExceeded as e:
        assert_contains(e.user_message, "активная проверка", "concurrent limit msg")
        q.release(user_id=7)
        return
    raise AssertionError("expected concurrent limit to fire")


async def t_queue_global_serialization() -> None:
    cfg = RateLimitConfig()
    q = JobQueue(cfg)
    # Acquire max_concurrent_global slots from distinct users
    for uid in range(cfg.max_concurrent_global):
        pos = await q.acquire(user_id=1000 + uid)
        assert_eq(pos, 0, f"first wave should not queue (uid {uid})")
    # Next acquire from a new user should *block* on the semaphore
    waiter = asyncio.create_task(q.acquire(user_id=9999))
    await asyncio.sleep(0.05)
    if waiter.done():
        raise AssertionError("global limit not enforced — extra job acquired immediately")
    # Release one slot, waiter should proceed
    q.release(user_id=1000)
    await asyncio.wait_for(waiter, timeout=2.0)
    # Cleanup
    for uid in range(1, cfg.max_concurrent_global):
        q.release(user_id=1000 + uid)
    q.release(user_id=9999)


# ───────────────── formatter tests ─────────────────


def _fake_repo() -> RepoInfo:
    return RepoInfo(
        platform="github", owner="o", repo="r",
        url="https://github.com/o/r", size_kb=10,
    )


def t_formatter_start_help() -> None:
    start = format_start_message()
    assert_contains(start, "Acton CI-Bot", "start branding")
    assert_contains(start, "acton build", "start mentions build")
    assert_contains(start, "fmt", "start mentions fmt")
    help_ = format_help_message()
    assert_contains(help_, "Format", "help mentions Format step")
    assert_contains(help_, "Acton.toml", "help mentions Acton.toml requirement")


def t_formatter_queue_position() -> None:
    assert_contains(format_queue_position(0), "Запускаю", "running message")
    assert_contains(format_queue_position(2), "#2", "queue position #2")


def t_formatter_all_pass() -> None:
    r = RunResult(repo=_fake_repo(), total_duration_s=4.2)
    r.steps = [
        StepResult("build", 0, "", "", 1.0),
        StepResult("test",  0, "", "", 2.0),
        StepResult("check", 0, "", "", 0.6),
        StepResult("fmt",   0, "", "", 0.6),
    ]
    out = format_report(r)
    assert_contains(out, "All checks passed", "happy summary present")
    for label in ("Build", "Tests", "Lint", "Format"):
        assert_contains(out, label, f"label {label} present")


def t_formatter_failure_with_skips() -> None:
    r = RunResult(repo=_fake_repo(), total_duration_s=0.4)
    r.steps = [
        StepResult("build", 1, "", "syntax error at line 3", 0.4),
        StepResult("test",  -1, "", "", 0.0, skipped=True),
        StepResult("check", -1, "", "", 0.0, skipped=True),
        StepResult("fmt",   -1, "", "", 0.0, skipped=True),
    ]
    out = format_report(r)
    assert_contains(out, "Failed:", "failure summary")
    assert_contains(out, "Build", "build labelled")
    assert_contains(out, "skipped", "skipped tag present")
    assert_contains(out, "syntax error", "stderr surfaced")


def t_formatter_not_an_acton_project() -> None:
    r = RunResult(repo=_fake_repo(), total_duration_s=1.2)
    r.steps = [
        StepResult(
            "build", 1, "",
            "Error: Acton.toml not found. Run 'acton init' to initialize Acton in the project.",
            1.2,
        ),
        StepResult("test", -1, "", "", 0, skipped=True),
        StepResult("check", -1, "", "", 0, skipped=True),
        StepResult("fmt", -1, "", "", 0, skipped=True),
    ]
    out = format_report(r)
    assert_contains(out, "Это не Acton-проект", "friendly not-acton message")
    if "Acton.toml not found" in out:
        raise AssertionError("raw error leaked into not-an-acton-project report")


def t_formatter_timeout() -> None:
    r = RunResult(repo=_fake_repo(), total_duration_s=180.0)
    r.steps = [
        StepResult("build", TIMEOUT_RETURN_CODE, "", "Timeout after 180s", 180.0),
        StepResult("test", -1, "", "", 0, skipped=True),
        StepResult("check", -1, "", "", 0, skipped=True),
        StepResult("fmt", -1, "", "", 0, skipped=True),
    ]
    out = format_report(r)
    assert_contains(out, "timed out", "timeout phrase")
    assert_contains(out, "⏰", "timeout emoji")
    assert_contains(out, "Timed out:", "timeout summary section")


def t_formatter_html_escaping() -> None:
    r = RunResult(repo=_fake_repo(), total_duration_s=0.1)
    r.steps = [StepResult("build", 1, "", "<script>alert(1)</script>", 0.1)]
    out = format_report(r)
    if "<script>" in out:
        raise AssertionError("html not escaped — XSS risk in Telegram message")
    assert_contains(out, "&lt;script&gt;", "html-escaped output")


def t_formatter_clone_error() -> None:
    r = RunResult(
        repo=_fake_repo(),
        total_duration_s=0.3,
        error="Git clone failed:\nfatal: repository not found",
    )
    out = format_report(r)
    assert_contains(out, "Error:", "clone error labelled")
    assert_contains(out, "repository not found", "clone error surfaced")


# ───────────────── subscriptions tests ─────────────────


def t_subs_crud() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = SubscriptionStore(os.path.join(d, "subs.db"))
        assert_eq(store.count(), 0, "fresh store is empty")
        assert_eq(store.add(100, "owner/repo", 1), True, "first add returns True")
        assert_eq(store.add(100, "owner/repo", 1), False, "duplicate add returns False")
        subs = store.list_for_chat(100)
        assert_eq(len(subs), 1, "one subscription listed")
        assert_eq(subs[0].repo_full_name, "owner/repo", "round-trip repo name")
        assert_eq(store.remove(100, "owner/repo"), True, "remove returns True")
        assert_eq(store.remove(100, "owner/repo"), False, "second remove returns False")


def t_subs_case_insensitive() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = SubscriptionStore(os.path.join(d, "subs.db"))
        store.add(7, "Owner/Repo", 1)
        chats = store.list_chats_for_repo("owner/repo")
        assert_eq(chats, [7], "lookup is case-insensitive")
        chats = store.list_chats_for_repo("OWNER/REPO")
        assert_eq(chats, [7], "lookup is case-insensitive (upper)")


def t_subs_multi_chat() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = SubscriptionStore(os.path.join(d, "subs.db"))
        store.add(1, "k/r", 100)
        store.add(2, "k/r", 100)
        store.add(3, "other/r", 100)
        chats = sorted(store.list_chats_for_repo("k/r"))
        assert_eq(chats, [1, 2], "two chats fan-out for k/r")
        assert_eq(store.list_chats_for_repo("other/r"), [3], "single chat for other/r")
        assert_eq(store.list_chats_for_repo("missing/x"), [], "empty list for unknown repo")


def t_parse_repo_arg() -> None:
    assert_eq(_parse_repo_arg("/subscribe owner/repo"), "owner/repo", "bare form")
    assert_eq(
        _parse_repo_arg("/subscribe https://github.com/owner/repo"),
        "owner/repo",
        "url form",
    )
    assert_eq(
        _parse_repo_arg("/subscribe https://github.com/owner/repo.git"),
        "owner/repo",
        "url with .git",
    )
    if _parse_repo_arg("/subscribe") is not None:
        raise AssertionError("missing arg should return None")
    if _parse_repo_arg("/subscribe https://gitlab.com/x/y") is not None:
        raise AssertionError("gitlab url should be rejected (github-only webhooks)")
    if _parse_repo_arg("/subscribe owner/repo;rm -rf /") is not None:
        raise AssertionError("must reject injection-y args")


# ───────────────── webhook tests ─────────────────


def t_webhook_signature() -> None:
    import hashlib, hmac as _hmac
    secret = "topsecret"
    body = b'{"hello":"world"}'
    good = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert_eq(_verify_signature(secret, body, good), True, "good signature passes")
    bad = "sha256=" + ("0" * 64)
    assert_eq(_verify_signature(secret, body, bad), False, "bad signature rejected")
    assert_eq(_verify_signature(secret, body, None), False, "missing header rejected")
    assert_eq(_verify_signature(secret, body, "wrong-prefix"), False, "wrong prefix rejected")


def _sample_pr_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "feat: add thing",
            "html_url": "https://github.com/k1borg32/acton-counter-demo/pull/42",
            "user": {"login": "k1borg32"},
            "head": {
                "ref": "feature-branch",
                "sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "repo": {
                    "name": "acton-counter-demo",
                    "clone_url": "https://github.com/k1borg32/acton-counter-demo.git",
                    "owner": {"login": "k1borg32"},
                },
            },
        },
        "repository": {"full_name": "k1borg32/acton-counter-demo"},
    }


def t_webhook_pr_event_parse() -> None:
    job = _parse_pr_event(_sample_pr_payload("opened"))
    if job is None:
        raise AssertionError("opened PR should produce a job")
    assert_eq(job.repo.full_name, "k1borg32/acton-counter-demo", "repo full_name")
    assert_eq(job.ref, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "head sha")
    assert_eq(job.pr_number, 42, "PR number")
    assert_eq(job.pr_author, "k1borg32", "PR author")
    assert_eq(job.repo.url, "https://github.com/k1borg32/acton-counter-demo", "clone url normalized")


def t_webhook_pr_action_filter() -> None:
    if _parse_pr_event(_sample_pr_payload("closed")) is not None:
        raise AssertionError("closed action must be filtered out")
    if _parse_pr_event(_sample_pr_payload("labeled")) is not None:
        raise AssertionError("labeled action must be filtered out")
    if _parse_pr_event({"action": "opened"}) is not None:
        raise AssertionError("missing pr block must be filtered out")
    for action in ("opened", "synchronize", "reopened"):
        if _parse_pr_event(_sample_pr_payload(action)) is None:
            raise AssertionError(f"{action} should produce a job")


# ───────────────── runner integration tests ─────────────────


def docker_image_works(image: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", image, "--version"],
            capture_output=True, text=True, timeout=30,
        )
        return out.returncode == 0 and "acton" in out.stdout.lower()
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


async def t_runner_image_smoke() -> None:
    cfg = RunnerConfig()
    if not docker_image_works(cfg.docker_image):
        raise AssertionError(f"docker image {cfg.docker_image} not runnable")


async def t_runner_tolk_bench_pipeline() -> None:
    """Real network → git clone → docker run. Verifies hardened clone and
    the not-an-Acton-project detection in the formatter."""
    from bot.services.validator import validate_repo

    repo = await validate_repo(
        "https://github.com/ton-blockchain/tolk-bench",
        max_size_kb=200_000,
    )
    cfg = RunnerConfig()
    result = await run_acton_pipeline(repo, cfg)
    if result.error is not None:
        raise AssertionError(f"unexpected clone error: {result.error}")
    rendered = format_report(result)
    assert_contains(rendered, "Это не Acton-проект", "tolk-bench → friendly msg")


async def t_runner_acton_new_happy_path() -> None:
    """Scaffold a counter project via `acton new` (no network needed,
    template is bundled), then drive run_acton_steps directly against it."""
    cfg = RunnerConfig()
    tmp = tempfile.mkdtemp(prefix="acton_e2e_")
    try:
        proj_parent = tmp.replace("\\", "/")
        scaffold = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", "none",
                "-v", f"{proj_parent}:/work",
                "-w", "/work",
                cfg.docker_image,
                "new", "--name", "e2eproj",
                "--template", "counter", "--license", "MIT",
                "proj",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if scaffold.returncode != 0:
            raise AssertionError(
                f"acton new failed rc={scaffold.returncode}\n{scaffold.stderr}"
            )
        project_dir = os.path.join(tmp, "proj")
        if not os.path.exists(os.path.join(project_dir, "Acton.toml")):
            raise AssertionError("scaffold did not produce Acton.toml")

        result = RunResult(repo=RepoInfo(
            platform="local", owner="e2e", repo="counter",
            url="local://e2e/counter",
        ))
        await run_acton_steps(project_dir, cfg, result)

        for step in result.steps:
            if not step.ok or step.skipped:
                raise AssertionError(
                    f"step {step.step} did not pass: rc={step.return_code} "
                    f"skipped={step.skipped} stderr={step.stderr[:200]!r}"
                )
        rendered = format_report(result)
        assert_contains(rendered, "All checks passed", "happy report")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_runner_timeout_path() -> None:
    """Exercise _run_subprocess timeout handling against a guaranteed-hung
    child (python sleep). We can't easily force a docker-run hang from
    outside, but this is the same code path that all step runs use."""
    code, stdout, stderr = await _run_subprocess(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=1,
    )
    assert_eq(code, TIMEOUT_RETURN_CODE, "timeout return code")
    assert_contains(stderr, "Timeout after", "timeout stderr message")


# ───────────────── runner ─────────────────


async def main() -> int:
    results: list[TestCase] = []

    section("validator")
    results.append(report_case("valid urls",       t_validator_valid_urls))
    results.append(report_case("reject bad urls",  t_validator_rejects_bad_urls))

    section("queue")
    results.append(await report_case_async("per-user hourly limit", t_queue_per_user_hourly_limit))
    results.append(await report_case_async("per-user concurrent",   t_queue_per_user_concurrent_limit))
    results.append(await report_case_async("global serialization",  t_queue_global_serialization))

    section("formatter")
    results.append(report_case("start/help",             t_formatter_start_help))
    results.append(report_case("queue position",         t_formatter_queue_position))
    results.append(report_case("all pass",               t_formatter_all_pass))
    results.append(report_case("failure with skips",     t_formatter_failure_with_skips))
    results.append(report_case("not an acton project",   t_formatter_not_an_acton_project))
    results.append(report_case("timeout rendering",      t_formatter_timeout))
    results.append(report_case("html escaping (xss)",    t_formatter_html_escaping))
    results.append(report_case("clone error rendering",  t_formatter_clone_error))

    section("subscriptions store")
    results.append(report_case("CRUD round-trip", t_subs_crud))
    results.append(report_case("case-insensitive repo names", t_subs_case_insensitive))
    results.append(report_case("multi-chat fan-out", t_subs_multi_chat))
    results.append(report_case("parse repo arg", t_parse_repo_arg))

    section("webhook")
    results.append(report_case("hmac signature verify", t_webhook_signature))
    results.append(report_case("pr event parsing", t_webhook_pr_event_parse))
    results.append(report_case("pr event filter (action)", t_webhook_pr_action_filter))

    section("runner integration")
    results.append(await report_case_async("image smoke",            t_runner_image_smoke))
    results.append(await report_case_async("tolk-bench end-to-end",  t_runner_tolk_bench_pipeline))
    results.append(await report_case_async("counter happy path",     t_runner_acton_new_happy_path))
    results.append(await report_case_async("timeout path",           t_runner_timeout_path))

    section("summary")
    passed = sum(1 for c in results if c.ok)
    failed = [c for c in results if not c.ok]
    print(f"  {passed}/{len(results)} passed")
    if failed:
        print("  FAILURES:")
        for c in failed:
            print(f"    - {c.name}: {c.detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
