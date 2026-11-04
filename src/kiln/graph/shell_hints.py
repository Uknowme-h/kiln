"""Instant guidance for common shell failures — before any model is asked.

SRE runbooks give known failures a canned remediation before a human is paged;
this is that for the developers. The hint is appended to the shell result the
developer reads; the orchestrator escalates to the tech lead only when the same
kind of failure recurs within one task.
"""

from __future__ import annotations

import re

_HINTS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "missing_command",
        re.compile(
            r"command not found|is not recognized as (an internal or external command|the name of a"
            r" cmdlet)|CommandNotFoundException|'\S+' is not recognized",
            re.IGNORECASE,
        ),
        "the command is not on PATH. Use the project's own tooling (npm run <script>, "
        "npx --no-install <bin>, the .venv interpreter); if the toolchain itself is missing, "
        "escalate(kind='environment').",
    ),
    (
        "port_in_use",
        re.compile(r"EADDRINUSE|address already in use|port \d+ is (already )?in use", re.IGNORECASE),
        "the port is taken. Start on another port through config or env (e.g. PORT=…); never "
        "kill processes you did not start.",
    ),
    (
        "permission",
        re.compile(r"EACCES|EPERM|Permission denied|Access is denied|UnauthorizedAccess", re.IGNORECASE),
        "permission denied. Never use sudo and write only inside the workspace; on Windows a "
        "file may be locked by a running process — stop your own dev server first.",
    ),
    (
        "timeout",
        re.compile(r"\[command exceeded .* and was stopped", re.IGNORECASE),
        "the command timed out. Run servers with background=true; tests must not wait on the "
        "network or on a server that never starts.",
    ),
    (
        "missing_module",
        re.compile(r"Cannot find module|ModuleNotFoundError|No module named|ERR_MODULE_NOT_FOUND", re.IGNORECASE),
        "a dependency or import is missing. Check the import path, then add the package to the "
        "manifest (package.json / requirements.txt) and install it.",
    ),
)


def shell_hint(output: str) -> tuple[str, str] | None:
    """(kind, hint) for a failed command's output, or None.

    Only failed commands get hints (a non-zero exit or a stop line), so a
    successful command that merely prints one of these words is left alone.
    """
    text = output or ""
    failed = "[exit code" in text or "[command exceeded" in text
    if not failed:
        return None
    for kind, pattern, hint in _HINTS:
        if pattern.search(text):
            return kind, hint
    return None
