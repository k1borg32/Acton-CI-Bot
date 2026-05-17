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
# never rewrites files. check uses JSON output for richer per-finding parsing.
# The test step args are extended by _run_acton_step when a gas-snapshot path
# is provided.
_STEP_ARGS: dict[str, list[str]] = {
    "build": ["build"],
    "test": ["test"],
    "check": ["check", "--output-format", "json"],
    "fmt": ["fmt", "--check"],
}


async def _run_acton_step(
    step: str,
    project_dir: str,
    config: RunnerConfig,
    gas_snapshot_path: str | None = None,
) -> StepResult:
    """Run a single Acton step inside a Docker container.

    If `gas_snapshot_path` is given AND step == "test", appends
    --snapshot <path> so the test runner emits a gas snapshot JSON.
    Path is interpreted inside the /workspace mount.
    """
    t0 = monotonic()

    # Docker Desktop on Windows accepts forward-slash drive paths
    # (e.g. C:/Users/... → mounted via the VM). On Linux it's a no-op.
    docker_project_dir = project_dir.replace("\\", "/")

    extra_args: list[str] = []
    if step == "test" and gas_snapshot_path:
        extra_args = ["--snapshot", gas_snapshot_path]

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
        *extra_args,
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


async def run_acton_adhoc(
    args: list[str],
    config: RunnerConfig,
    *,
    project_dir: str | None = None,
    timeout: int | None = None,
    allow_network: bool = False,
) -> tuple[int, str, str]:
    """Run an ad-hoc `acton <args>` inside a fresh container.

    Used by /disasm, /wrapper, /verify which don't fit the build→test→check
    pipeline shape. `project_dir`, if given, is bind-mounted at /workspace.
    `allow_network` relaxes --network=none (needed for `disasm --address`
    or any subcommand that fetches from the blockchain / dependencies).
    """
    timeout = timeout or config.build_timeout
    cmd: list[str] = [
        "docker", "run", "--rm",
        "--memory", config.container_memory,
        "--cpus", config.container_cpus,
        "--pids-limit", config.container_pids_limit,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:size=128m,exec",
    ]
    if not allow_network:
        cmd.extend(["--network", "none"])
    if project_dir:
        docker_pd = project_dir.replace("\\", "/")
        cmd.extend(["-v", f"{docker_pd}:/workspace", "-w", "/workspace"])
    cmd.append(config.docker_image)
    cmd.extend(args)
    return await _run_subprocess(cmd, timeout=timeout)


async def run_acton_steps(
    project_dir: str,
    config: RunnerConfig,
    result: RunResult,
    gas_snapshot_path: str | None = None,
) -> None:
    """Run build → test → check → fmt against an already-prepared project dir.

    Mutates `result` in-place. `test`/`check`/`fmt` are skipped if `build`
    fails. Exposed separately so tests can drive the pipeline without a
    git clone.

    If `gas_snapshot_path` is given, it's passed to the `test` step so Acton
    writes a gas snapshot JSON there. Path is relative to /workspace (e.g.
    "/workspace/gas.json") because that's the in-container view.
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
        step_result = await _run_acton_step(
            step_name, project_dir, config,
            gas_snapshot_path=gas_snapshot_path,
        )
        result.steps.append(step_result)

        if step_name == "build" and not step_result.ok:
            build_failed = True
            logger.warning("Build failed in %s, skipping remaining steps", project_dir)


async def run_acton_pipeline(
    repo: RepoInfo,
    config: RunnerConfig,
    ref: str | None = None,
    gas_snapshot_host_path: str | None = None,
) -> RunResult:
    """
    Full Acton CI pipeline:
      0. git clone (hardened) — optionally pinned to `ref` (branch/tag/SHA)
      1. acton build
      2. acton test  (skipped if build fails)
      3. acton check (skipped if build fails)
      4. acton fmt --check (skipped if build fails)

    Each step runs in a fresh ephemeral Docker container.

    If `gas_snapshot_host_path` is given, the test step writes its gas
    snapshot there (a host path that will also be visible inside the
    runner container via the /tmp:/tmp bind mount). After the pipeline
    finishes the caller can read that JSON to do PR base↔head diffing.
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

        # The gas snapshot path lives under /workspace so it's reachable
        # from inside the runner container. Write it into the cloned project
        # dir (which IS /workspace inside the container).
        in_container_snapshot = None
        if gas_snapshot_host_path is not None:
            snapshot_name = Path(gas_snapshot_host_path).name
            in_container_snapshot = f"/workspace/{snapshot_name}"
            # also copy into the project dir on disk via the same name
            # so the host can find it at $project_dir/$snapshot_name
        await run_acton_steps(
            project_dir, config, result,
            gas_snapshot_path=in_container_snapshot,
        )

        # If a snapshot was requested, move it from the project dir to the
        # caller-specified host path (project dir is about to be deleted).
        if gas_snapshot_host_path is not None and in_container_snapshot is not None:
            src = Path(project_dir) / Path(in_container_snapshot).name
            if src.exists():
                try:
                    shutil.copyfile(src, gas_snapshot_host_path)
                except OSError as e:
                    logger.warning("Failed to persist gas snapshot: %s", e)

    result.total_duration_s = round(monotonic() - t0, 1)
    return result
