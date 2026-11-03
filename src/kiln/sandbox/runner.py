"""Docker command for one test run. Execution is separate so tests can inspect the args."""

import subprocess
import uuid
from pathlib import Path

from kiln.config import Settings
from kiln.sandbox.parser import parse_junit
from kiln.schemas.results import TestReport

RESULTS_NAME = "kiln-results.xml"
VENDOR_DIR = "vendor"
DEPS_DIR = ".deps"


class SandboxError(RuntimeError):
    """The sandbox did not produce a test report."""


def _has_vendored_deps(workspace: Path) -> bool:
    vendor = workspace / VENDOR_DIR
    if not vendor.is_dir():
        return False
    return any(vendor.glob("*.whl")) or any(vendor.glob("*.tar.gz"))


def build_docker_args(workspace: Path, settings: Settings, name: str) -> list[str]:
    """Args that keep generated code off the host network and away from secrets.

    When the companion vendored the app's requirements on the host, the
    sandbox installs them offline into /workspace/.deps (the root filesystem
    stays read-only) and puts them on PYTHONPATH before running pytest.
    """
    mount = workspace.resolve().as_posix()
    command = f"pytest -q --junitxml=/workspace/{RESULTS_NAME}"
    pythonpath = "/workspace"
    if _has_vendored_deps(workspace):
        pythonpath = f"/workspace/{DEPS_DIR}:/workspace"
        command = (
            f"pip install -q --no-index --find-links /workspace/{VENDOR_DIR} "
            f"--target /workspace/{DEPS_DIR} -r /workspace/requirements.txt && {command}"
        )
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
        "-e",
        f"PYTHONPATH={pythonpath}",
        "-v",
        f"{mount}:/workspace:rw",
        "-w",
        "/workspace",
        settings.sandbox_image,
        "sh",
        "-c",
        command,
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
