"""Docker command for one test run. Execution is separate so tests can inspect the args."""

import subprocess
import uuid
from pathlib import Path

from kiln.config import Settings
from kiln.sandbox.parser import parse_junit
from kiln.schemas.results import TestReport

RESULTS_NAME = "kiln-results.xml"


class SandboxError(RuntimeError):
    """The sandbox did not produce a test report."""


def build_docker_args(workspace: Path, settings: Settings, name: str) -> list[str]:
    """Args that keep generated code off the host network and away from secrets."""
    mount = workspace.resolve().as_posix()
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--memory",
        settings.sandbox_memory,
        "--cpus",
        settings.sandbox_cpus,
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp",
        "-v",
        f"{mount}:/workspace:rw",
        "-w",
        "/workspace",
        settings.sandbox_image,
        "pytest",
        "-q",
        f"--junitxml=/workspace/{RESULTS_NAME}",
    ]


def run_tests(workspace: Path, settings: Settings) -> TestReport:
    if not workspace.is_dir():
        raise SandboxError(f"workspace does not exist: {workspace}")
    name = f"kiln-{uuid.uuid4().hex[:8]}"
    args = build_docker_args(workspace, settings, name)
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=settings.sandbox_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "kill", name], capture_output=True, check=False)
        raise SandboxError("sandbox hit the time limit") from exc
    except FileNotFoundError as exc:
        raise SandboxError("docker is not on PATH") from exc

    results = workspace / RESULTS_NAME
    if not results.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SandboxError(detail or f"docker exited {completed.returncode} without results")
    report = parse_junit(results)
    log = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return report.model_copy(update={"raw_log": report.raw_log or log})
