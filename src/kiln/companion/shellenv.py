"""What machine is this, and which shell will run the agents' commands.

A port of the idea in OpenCode's shell selection: pick the shell the way a
developer on that OS would expect, build the right argv for it, and tell the
model the truth about where its commands run. Detection never executes
anything; it only looks at the platform and at PATH.

Windows prefers PowerShell 7, then Windows PowerShell, then Git Bash, then
cmd. macOS prefers the user's shell, then zsh. Linux prefers the user's shell,
then bash, then sh. fish and nushell are skipped: agents write POSIX syntax.
"""

import os
import platform
import shutil
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

ShellKind = Literal["powershell", "cmd", "posix"]

# fish and nu are real shells but agents write POSIX; commands would misparse
INCOMPATIBLE_SHELLS = frozenset({"fish", "nu", "nushell"})
POSIX_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "ksh"})

# Looked up on PATH so the prompt can say what is installed. Nothing is run.
DETECTED_TOOLS = (
    "python",
    "python3",
    "py",
    "pip",
    "node",
    "npm",
    "npx",
    "git",
    "docker",
    "winget",
    "choco",
    "scoop",
    "brew",
    "apt-get",
    "dnf",
    "pacman",
)

Which = Callable[[str], str | None]


@dataclass(frozen=True)
class ShellInfo:
    path: str
    name: str  # pwsh, powershell, cmd, bash, zsh, sh, ...
    kind: ShellKind
    label: str  # what to call it when talking to the user or the model


def os_label(system: str | None = None) -> str:
    system = system or platform.system()
    return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(system, system)


def _name(path: str) -> str:
    return Path(path.replace("\\", "/")).stem.lower()


def _git_bash(which: Which) -> str | None:
    git = which("git")
    if not git:
        return None
    candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
    return str(candidate) if candidate.is_file() else None


def select_shell(
    system: str | None = None,
    *,
    which: Which = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> ShellInfo:
    """Pick the shell commands run in, the way `ShellSelect` does in OpenCode."""
    system = system or platform.system()
    environ = os.environ if environ is None else environ

    if system == "Windows":
        pwsh = which("pwsh")
        if pwsh:
            return ShellInfo(pwsh, "pwsh", "powershell", "PowerShell 7")
        powershell = which("powershell")
        if powershell:
            return ShellInfo(powershell, "powershell", "powershell", "Windows PowerShell 5.1")
        bash = _git_bash(which)
        if bash:
            return ShellInfo(bash, "bash", "posix", "Git Bash")
        return ShellInfo(environ.get("COMSPEC") or "cmd.exe", "cmd", "cmd", "Command Prompt")

    user_shell = environ.get("SHELL", "")
    name = _name(user_shell) if user_shell else ""
    if name in POSIX_SHELLS and (os.path.isfile(user_shell) or which(name)):
        return ShellInfo(user_shell if os.path.isfile(user_shell) else which(name), name, "posix", name)
    if system == "Darwin":
        return ShellInfo("/bin/zsh", "zsh", "posix", "zsh")
    bash = which("bash")
    if bash:
        return ShellInfo(bash, "bash", "posix", "bash")
    return ShellInfo("/bin/sh", "sh", "posix", "sh")


def shell_argv(shell: ShellInfo, command: str) -> list[str]:
    """The argv that runs one command string in this shell, non-interactively."""
    if shell.kind == "cmd":
        return [shell.path, "/d", "/s", "/c", command]
    if shell.kind == "powershell":
        return [shell.path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    return [shell.path, "-c", command]


@dataclass
class EnvironmentInfo:
    os: str
    os_version: str
    arch: str
    shell: ShellInfo
    home: str
    tools: dict[str, str | None] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.os} {self.os_version} ({self.arch}), shell: {self.shell.label}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["shell"] = asdict(self.shell)
        return data

    def has(self, *names: str) -> bool:
        return any(self.tools.get(name) for name in names)


def detect_environment(
    system: str | None = None,
    *,
    which: Which = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> EnvironmentInfo:
    system = system or platform.system()
    return EnvironmentInfo(
        os=os_label(system),
        os_version=platform.release(),
        arch=platform.machine() or "unknown",
        shell=select_shell(system, which=which, environ=environ),
        home=str(Path.home()),
        tools={name: which(name) for name in DETECTED_TOOLS},
    )


def refresh_windows_path() -> None:
    """Re-read PATH from the registry so a tool installed a moment ago is visible.

    A child process only inherits the environment it started with, so right
    after `winget install` the companion cannot see the new tool until PATH is
    re-read. Merges machine and user PATH ahead of the current value.
    """
    if platform.system() != "Windows":
        return
    try:
        import winreg
    except ImportError:  # pragma: no cover - only on Windows
        return

    def read(root, subkey) -> str:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                return os.path.expandvars(value)
        except OSError:
            return ""

    machine = read(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    user = read(winreg.HKEY_CURRENT_USER, "Environment")
    seen: set[str] = set()
    merged: list[str] = []
    for part in (machine + ";" + user + ";" + os.environ.get("PATH", "")).split(";"):
        key = part.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(part.strip())
    os.environ["PATH"] = ";".join(merged)
