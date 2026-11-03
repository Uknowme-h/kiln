"""Run a command on the user's machine, with limits.

Every run gets a working directory pinned by the caller, a timeout, a cap on
how much output comes back, a scrubbed environment, and a kill of the whole
process tree when it is stopped. Callers decide *whether* a command may run
(permissions and approval); this module only runs it well.
"""

import os
import re
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from kiln.companion.shellenv import ShellInfo, shell_argv

DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 30_000

# Variables that look like credentials never reach an agent's command.
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)


@dataclass
class CommandResult:
    exit_code: int | None
    output: str
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def render(self, timeout_seconds: float | None = None) -> str:
        """The text the agent sees: output plus an honest status line."""
        parts = [self.output.rstrip()] if self.output.strip() else []
        if self.truncated:
            parts.append("[output was truncated]")
        if self.timed_out:
            limit = f"{timeout_seconds:g}s" if timeout_seconds else "the limit"
            parts.append(
                f"[command exceeded {limit} and was stopped; retry with a longer timeout "
                "if it is expected to take that long]"
            )
        elif self.exit_code != 0:
            parts.append(f"[exit code {self.exit_code}]")
        return "\n".join(parts) if parts else "(no output)"


def scrubbed_env(base: dict[str, str] | None = None, extra: dict[str, str] | None = None) -> dict:
    env = {
        name: value
        for name, value in (os.environ if base is None else base).items()
        if not _SECRET_NAME.search(name)
    }
    env.update({"AGENT": "1", "KILN": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(extra or {})
    return env


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    head = MAX_OUTPUT_CHARS * 2 // 3
    tail = MAX_OUTPUT_CHARS - head
    return f"{text[:head]}\n...[{len(text) - MAX_OUTPUT_CHARS} characters omitted]...\n{text[-tail:]}", True


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop the process and everything it started."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def run_argv(
    argv: list[str],
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict | None = None,
) -> CommandResult:
    """Run an argv directly (no shell, so no quoting to get wrong)."""
    resolved = shutil.which(argv[0], path=(env or os.environ).get("PATH"))
    if resolved is None and not os.path.isabs(argv[0]):
        return CommandResult(127, f"command not found: {argv[0]}")
    argv = [resolved or argv[0], *argv[1:]]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env if env is not None else scrubbed_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **_popen_kwargs(),
        )
    except OSError as exc:
        return CommandResult(126, f"could not start {argv[0]}: {exc}")
    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            raw, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            raw = b""
    text, truncated = _clip((raw or b"").decode("utf-8", errors="replace"))
    return CommandResult(proc.returncode, text, timed_out=timed_out, truncated=truncated)


def run_shell(
    command: str,
    shell: ShellInfo,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict | None = None,
) -> CommandResult:
    """Run one command string in the chosen shell."""
    return run_argv(shell_argv(shell, command), cwd, timeout, env)


class BackgroundJobs:
    """Long-running processes (dev servers) the agent started; all die with the companion."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._jobs: dict[str, tuple[subprocess.Popen, Path]] = {}

    def start(self, argv: list[str], cwd: Path, env: dict | None = None) -> tuple[str, Path]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex[:8]
        log_path = self.log_dir / f"{job_id}.log"
        handle = open(log_path, "wb")  # noqa: SIM115 - owned by the process until it exits
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env if env is not None else scrubbed_env(),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                **_popen_kwargs(),
            )
        finally:
            handle.close()
        self._jobs[job_id] = (proc, log_path)
        return job_id, log_path

    def stop_all(self) -> None:
        for proc, _ in self._jobs.values():
            kill_tree(proc)
        self._jobs.clear()
