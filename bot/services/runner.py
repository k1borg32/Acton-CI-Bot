"""
Docker-based Acton runner.

Clones a repo, spins up an ephemeral Docker container with the official
Acton image, runs build/test/check/fmt steps, and returns structured results.

Security measures:
  - --no-recurse-submodules to prevent malicious submodule hooks
  - core.symlinks=false to block symlink attacks
  - core.hooksPath=/dev/null to disable git hooks
  - protocol.file.allow=never to block file:// URLs
  - Docker: --network=none, --memory, --cpus, --pids-limit, --rm
  - Tempdir per job, auto-cleaned
  - Non-root user inside the container
"""

import asyncio
import contextlib
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from bot.config import RunnerConfig
from bot.services.parsers import summarize
from bot.services.validator import RepoInfo

logger = logging.getLogger(__name__)

# Sentinel return code for steps killed by the build/clone timeout.
TIMEOUT_RETURN_CODE = -100


def _force_remove_readonly(func, path, _exc):
    """rmtree onerror: chmod and retry — needed for files written by the
    in-container UID under Docker Desktop, and for git's read-only objects.
    """
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)


@contextlib.contextmanager
def _scoped_tempdir(prefix: str):
    """Like tempfile.TemporaryDirectory but never raises on cleanup."""
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        try:
            shutil.rmtree(path, onerror=_force_remove_readonly)
        except OSError as e:
            logger.warning("Failed to remove tempdir %s: %s", path, e)


@dataclass
class StepResult:
    """Result of a single Acton CLI step."""

    step: str
    return_code: int
    stdout: str
    stderr: str
    duration_s: float
    skipped: bool = False
    summary: str | None = None  # short human-readable extract, e.g. "8 passed in 1 file"

    @property
    def ok(self) -> bool:
        return self.return_code == 0

    @property
    def timed_out(self) -> bool:
        return self.return_code == TIMEOUT_RETURN_CODE


@dataclass
class RunResult:
    """Aggregated result of all Acton steps."""

    repo: RepoInfo
    steps: list[StepResult] = field(default_factory=list)
    total_duration_s: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        return all(s.ok or s.skipped for s in self.steps) and self.error is None


async def _run_subprocess(
    cmd: list[str],
    timeout: int,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Run a subprocess asynchronously with timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return TIMEOUT_RETURN_CODE, "", f"Timeout after {timeout}s"

    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace")[-8192:],  # cap output
        stderr_bytes.decode("utf-8", errors="replace")[-8192:],
    )


async def _clone_repo(
    repo: RepoInfo,
    dest: str,
    timeout: int,
    ref: str | None = None,
) -> tuple[int, str, str]:
    """Clone a repository with security hardening. If `ref` is given (a
    branch name, tag, or SHA), the clone is restricted to that ref."""
    cmd = [
        "git", "clone",
        "--depth=1",
        "--no-recurse-submodules",
        "--config", "core.symlinks=false",
        "--config", "core.hooksPath=/dev/null",
        "--config", "protocol.file.allow=never",
        # Preserve byte-for-byte file contents. Without this, Windows hosts
        # rewrite LF→CRLF on checkout, which makes `acton fmt --check` (and
        # any other byte-sensitive tool) report false failures.
        "--config", "core.autocrlf=false",
        "--config", "core.eol=lf",
    ]
    if ref is not None:
        # --branch accepts either a branch/tag name; for a raw SHA we fall
        # back to clone + fetch + checkout below.
        cmd.extend(["--branch", ref, "--single-branch"])
    else:
        cmd.append("--single-branch")
    cmd.extend([repo.url, dest])

    code, stdout, stderr = await _run_subprocess(cmd, timeout=timeout)
    if code != 0 and ref is not None and _looks_like_sha(ref):
        # `git clone --branch <SHA>` fails — retry with default branch then
        # fetch the SHA explicitly.
        code, stdout, stderr = await _run_subprocess(
            ["git", "clone", "--depth=1", "--no-recurse-submodules",
             "--config", "core.symlinks=false",
             "--config", "core.hooksPath=/dev/null",
             "--config", "protocol.file.allow=never",
             "--config", "core.autocrlf=false",
             "--config", "core.eol=lf",
             repo.url, dest],
            timeout=timeout,
        )
        if code == 0:
            code, stdout, stderr = await _run_subprocess(
                ["git", "-C", dest, "fetch", "--depth=1", "origin", ref],
                timeout=timeout,
            )
            if code == 0:
                code, stdout, stderr = await _run_subprocess(
                    ["git", "-C", dest, "checkout", "--detach", ref],
                    timeout=timeout,
                )
    return code, stdout, stderr


def _looks_like_sha(ref: str) -> bool:
    return len(ref) >= 7 and len(ref) <= 40 and all(c in "0123456789abcdef" for c in ref.lower())


# Pipeline step → Acton CLI args. fmt runs with --check so it only reports,
# never rewrites files.
_STEP_ARGS: dict[str, list[str]] = {
    "build": ["build"],
    "test": ["test"],
    "check": ["check"],
    "fmt": ["fmt", "--check"],
}


async def _run_acton_step(
    step: str,
    project_dir: str,
    config: RunnerConfig,
) -> StepResult:
    """Run a single Acton step inside a Docker container."""
    t0 = monotonic()

    # Docker Desktop on Windows accepts forward-slash drive paths
    # (e.g. C:/Users/... → mounted via the VM). On Linux it's a no-op.
    docker_project_dir = project_dir.replace("\\", "/")

    # Mount is RW because `acton build` writes artifacts into the project
    # directory. Containment comes from --network=none, --rm, --pids-limit,
    # --memory, --cpus, --cap-drop, --security-opt=no-new-privileges, and
    # the project dir being an ephemeral host tempdir.
    cmd = [
        "docker", "run", "--rm",
        "--memory", config.container_memory,
        "--cpus", config.container_cpus,
        "--pids-limit", config.container_pids_limit,
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:size=128m,exec",
        "-v", f"{docker_project_dir}:/workspace",
        "-w", "/workspace",
        config.docker_image,
        *_STEP_ARGS[step],
    ]

    return_code, stdout, stderr = await _run_subprocess(
        cmd, timeout=config.build_timeout
    )
    duration = monotonic() - t0

    ok = return_code == 0
    return StepResult(
        step=step,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=round(duration, 1),
        summary=summarize(step, stdout, stderr, ok),
    )


_PIPELINE_STEPS = ("build", "test", "check", "fmt")


async def run_acton_steps(
    project_dir: str,
    config: RunnerConfig,
    result: RunResult,
) -> None:
    """Run build → test → check → fmt against an already-prepared project dir.

    Mutates `result` in-place. `test`/`check`/`fmt` are skipped if `build`
    fails. Exposed separately so tests can drive the pipeline without a
    git clone.
    """
    build_failed = False
    for step_name in _PIPELINE_STEPS:
        if build_failed and step_name != "build":
            result.steps.append(
                StepResult(
                    step=step_name,
                    return_code=-1,
                    stdout="",
                    stderr="",
                    duration_s=0,
                    skipped=True,
                )
            )
            continue

        logger.info("Running acton %s in %s", step_name, project_dir)
        step_result = await _run_acton_step(step_name, project_dir, config)
        result.steps.append(step_result)

        if step_name == "build" and not step_result.ok:
            build_failed = True
            logger.warning("Build failed in %s, skipping remaining steps", project_dir)


async def run_acton_pipeline(
    repo: RepoInfo,
    config: RunnerConfig,
    ref: str | None = None,
) -> RunResult:
    """
    Full Acton CI pipeline:
      0. git clone (hardened) — optionally pinned to `ref` (branch/tag/SHA)
      1. acton build
      2. acton test  (skipped if build fails)
      3. acton check (skipped if build fails)
      4. acton fmt --check (skipped if build fails)

    Each step runs in a fresh ephemeral Docker container.
    """
    result = RunResult(repo=repo)
    t0 = monotonic()

    with _scoped_tempdir(prefix="acton_") as tmpdir:
        project_dir = str(Path(tmpdir) / "project")

        logger.info("Cloning %s ref=%s into %s", repo.url, ref or "<default>", project_dir)
        code, stdout, stderr = await _clone_repo(
            repo, project_dir, config.clone_timeout, ref=ref
        )

        if code != 0:
            result.error = f"Git clone failed:\n{stderr or stdout}"
            result.total_duration_s = round(monotonic() - t0, 1)
            logger.error("Clone failed for %s: %s", repo.full_name, stderr)
            return result

        await run_acton_steps(project_dir, config, result)

    result.total_duration_s = round(monotonic() - t0, 1)
    return result
