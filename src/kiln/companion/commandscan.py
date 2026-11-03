"""Read a command line before the user is asked about it.

The approval prompt must show what will actually run, so a compound command
(`a && b | c; d`) is split into one line per command, each classified:

- `run`          an ordinary command
- `install`      installs or upgrades software, or downloads packages
- `network`      talks to the network in other ways
- `destructive`  could damage the machine; refused outright

Anything the scanner cannot read with confidence (command substitution,
`eval`, `Invoke-Expression`, a nested shell, an unterminated quote) makes the
whole command *opaque*: it is shown raw and can never be remembered. The shape
follows OpenCode's `shell/parse.ts`, kept small and in plain Python.
"""

import re
import shlex
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["run", "install", "network", "destructive"]

# How many leading words name a command for reuse ("npm run *", "git status *").
ARITY: dict[str, int] = {
    "cat": 1,
    "ls": 1,
    "echo": 1,
    "pytest": 1,
    "node": 1,
    "python": 2,
    "py": 2,
    "pip": 2,
    "npm": 2,
    "npm run": 3,
    "npx": 2,
    "git": 2,
    "git remote": 3,
    "docker": 2,
    "docker compose": 3,
    "winget": 2,
    "brew": 2,
    "apt": 2,
    "apt-get": 2,
    "cargo": 2,
    "yarn": 2,
    "pnpm": 2,
}

_INSTALL_SUBCOMMANDS: dict[str, set[str]] = {
    "pip": {"install"},
    "pip3": {"install"},
    "pipx": {"install"},
    "uv": {"add", "sync", "pip"},
    "poetry": {"add", "install"},
    "npm": {"install", "i", "ci", "add", "update"},
    "yarn": {"add", "install", "upgrade"},
    "pnpm": {"add", "install", "i", "update"},
    "bun": {"add", "install"},
    "winget": {"install", "upgrade"},
    "choco": {"install", "upgrade"},
    "scoop": {"install", "update"},
    "brew": {"install", "upgrade", "reinstall"},
    "apt": {"install", "upgrade", "dist-upgrade"},
    "apt-get": {"install", "upgrade", "dist-upgrade"},
    "dnf": {"install", "upgrade"},
    "yum": {"install", "update"},
    "zypper": {"install", "update"},
    "snap": {"install", "refresh"},
    "nvm": {"install"},
    "fnm": {"install"},
    "volta": {"install"},
    "cargo": {"install", "add"},
    "go": {"install", "get"},
    "gem": {"install"},
}
_NETWORK_HEADS = {
    "curl", "wget", "invoke-webrequest", "iwr", "irm", "invoke-restmethod",
    "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet",
}  # fmt: skip
_NETWORK_GIT = {"clone", "fetch", "pull", "push", "remote"}
_SHELL_HEADS = {"sh", "bash", "zsh", "dash", "pwsh", "powershell", "cmd", "iex", "invoke-expression"}
_LOW_RISK_HEADS = {
    "pytest", "py.test", "ls", "dir", "pwd", "echo", "where", "which", "get-childitem", "gci",
}  # fmt: skip
_LOW_RISK_GIT = {"status", "diff", "log", "branch", "rev-parse"}

_DESTRUCTIVE = [
    # POSIX: recursive removal of root, home, or everything
    re.compile(r"\brm\s+(-\S+\s+)*-\S*[rR]\S*\s+(-\S+\s+)*(/|~|\$home|/\*|\*|\.)(\s|$)", re.I),
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r"\bdd\s+.*\bof=/dev/"),
    re.compile(r":\(\)\s*\{"),  # fork bomb
    re.compile(r"\bchmod\s+(-\S+\s+)*-R\s+\S+\s+/(\s|$)"),
    # Windows: formatting, recursive delete of a drive root or the profile
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\b(rd|rmdir)\s+(/\S+\s+)*/s\b.*\b([a-z]:\\?(\s|$)|%userprofile%|%homepath%)", re.I),
    re.compile(r"\bdel\s+(/\S+\s+)*[a-z]:\\?(\*|\s|$)", re.I),
    re.compile(
        r"\bremove-item\b.*-recurse.*(\s[a-z]:\\?(\s|$)|~|\$home|\$env:userprofile|\s\*(\s|$))", re.I
    ),
    re.compile(r"\b(diskpart|bcdedit|shutdown)\b", re.I),
    re.compile(r"\breg\s+delete\s+hk(lm|cu)\b", re.I),
]

_SEPARATORS = {"&&", "||", "|", ";", "&", "\n"}


@dataclass(frozen=True)
class Segment:
    text: str
    head: str
    kind: Kind
    pattern: str  # reusable "npm run *" style pattern, for logs and the prompt hint
    low_risk: bool = False


@dataclass
class Scan:
    command: str
    segments: list[Segment] = field(default_factory=list)
    opaque: bool = False
    reason: str = ""

    @property
    def destructive(self) -> bool:
        return any(segment.kind == "destructive" for segment in self.segments)

    @property
    def installs(self) -> bool:
        return any(segment.kind == "install" for segment in self.segments)

    @property
    def network(self) -> bool:
        return any(segment.kind in ("install", "network") for segment in self.segments)

    @property
    def lines(self) -> list[str]:
        """What the prompt shows: one line per command, or the raw text when opaque."""
        if self.opaque or not self.segments:
            return [self.command.strip()]
        return [segment.text for segment in self.segments]

    @property
    def rememberable(self) -> bool:
        """May the user say 'allow for this run' for this exact command?"""
        return (
            not self.opaque
            and bool(self.segments)
            and all(segment.kind == "run" and segment.low_risk for segment in self.segments)
        )


def _split(command: str, shell_kind: str) -> tuple[list[str], str]:
    """Split on top-level separators; returns (segments, opaque_reason)."""
    escape = {"powershell": "`", "cmd": "^"}.get(shell_kind, "\\")
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)

    def flush() -> None:
        text = "".join(current).strip()
        if text:
            parts.append(text)
        current.clear()

    while i < n:
        char = command[i]
        if quote:
            current.append(char)
            if char == escape and quote == '"' and shell_kind != "cmd" and i + 1 < n:
                current.append(command[i + 1])
                i += 1
            elif char == quote:
                quote = None
            i += 1
            continue
        if char == escape and i + 1 < n:
            if shell_kind == "posix" and char == "\\" and command[i + 1] == "\n":
                i += 2
                continue
            current.append(char)
            current.append(command[i + 1])
            i += 2
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            i += 1
            continue
        pair = command[i : i + 2]
        if pair in ("&&", "||"):
            flush()
            i += 2
            continue
        if char in (";", "\n", "|"):
            flush()
            i += 1
            continue
        if char == "&":
            # PowerShell's call operator (& "C:\tool.exe") is not a separator
            if shell_kind == "powershell" and not "".join(current).strip():
                current.append(char)
            elif i + 1 < n and command[i + 1] == ">":  # &> redirect
                current.append(char)
            else:
                flush()
            i += 1
            continue
        current.append(char)
        i += 1
    if quote:
        return parts, "unterminated quote"
    flush()
    return parts, ""


def _opaque_reason(command: str, shell_kind: str) -> str:
    # look outside single quotes only: inside them nothing is expanded in POSIX shells
    outside = re.sub(r"'[^']*'", "''", command) if shell_kind != "powershell" else command
    if "$(" in outside:
        return "command substitution"
    if shell_kind == "posix" and ("`" in outside or "<(" in outside or ">(" in outside):
        return "command substitution"
    if re.search(r"(?i)(^|[\s;|&])(iex|invoke-expression)\b", outside):
        return "Invoke-Expression"
    if re.search(r"(?i)\s-enc(odedcommand)?\b", outside):
        return "encoded command"
    if re.search(r"(?i)(^|[\s;|&])eval\b", outside):
        return "eval"
    return ""


def _tokens(text: str, shell_kind: str) -> list[str]:
    try:
        return shlex.split(text, posix=(shell_kind == "posix"))
    except ValueError:
        return text.split()


def _head(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower().strip("'\"")
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _pattern(tokens: list[str]) -> str:
    lowered = [t.lower() for t in tokens]
    for length in range(min(len(lowered), 3), 0, -1):
        arity = ARITY.get(" ".join(lowered[:length]))
        if arity is not None:
            return " ".join(tokens[:arity]) + " *"
    return f"{tokens[0]} *" if tokens else "*"


def _classify(text: str, tokens: list[str], head: str) -> tuple[Kind, bool]:
    lowered = text.lower()
    if any(pattern.search(lowered) for pattern in _DESTRUCTIVE):
        return "destructive", False
    args = [t.lower() for t in tokens[1:]]
    # `python -m pip install ...` and `py -m pip ...` are installs too
    if head in ("python", "python3", "py") and args[:2] == ["-m", "pip"]:
        head, args = "pip", args[2:]
    if head == "uv" and args[:1] == ["pip"]:
        args = args[1:]
        head = "pip"
    subcommands = _INSTALL_SUBCOMMANDS.get(head)
    if subcommands and args and args[0] in subcommands:
        return "install", False
    if head == "npx" and "--no-install" not in args:
        return "install", False
    if head == "dotnet" and args[:2] in (["add", "package"], ["tool", "install"]):
        return "install", False
    if head in _NETWORK_HEADS or (head == "git" and args[:1] and args[0] in _NETWORK_GIT):
        return "network", False
    low = (
        head in _LOW_RISK_HEADS
        or (head == "git" and bool(args) and args[0] in _LOW_RISK_GIT)
        or (head in ("python", "python3", "py") and args[:2] == ["-m", "pytest"])
        or (head == "npm" and args[:1] == ["test"])
        or (head == "npm" and args[:2] == ["run", "test"])
        or (head == "npx" and "vitest" in args)
        or (head == "node" and args[:1] == ["--check"])
        or (head == "tsc" and "--noemit" in args)
        or (len(args) == 1 and args[0] in ("--version", "-v", "-V", "version"))
    )
    return "run", low


def scan_command(command: str, shell_kind: str = "posix") -> Scan:
    """Split and classify a command line for the approval prompt."""
    scan = Scan(command=command)
    if not command.strip():
        scan.opaque, scan.reason = True, "empty command"
        return scan
    parts, problem = _split(command, shell_kind)
    reason = problem or _opaque_reason(command, shell_kind)
    segments: list[Segment] = []
    for part in parts:
        tokens = _tokens(part, shell_kind)
        # `sudo`, `time`, `nohup` and PowerShell's call operator do not change what runs
        while tokens and _head(tokens[0]) in ("sudo", "time", "nohup", "&", "command"):
            tokens = tokens[1:]
        if not tokens:
            continue
        head = _head(tokens[0])
        if head in _SHELL_HEADS and any(
            t.lower() in ("-c", "/c", "-command", "-encodedcommand") for t in tokens[1:]
        ):
            reason = reason or "nested shell"
        kind, low = _classify(" ".join(tokens), tokens, head)
        segments.append(Segment(" ".join(tokens), head, kind, _pattern(tokens), low))
    # a download piped into a shell runs unreviewed remote code
    heads = [segment.head for segment in segments]
    for index, segment in enumerate(segments):
        if (
            segment.kind in ("network", "install")
            and index + 1 < len(segments)
            and heads[index + 1] in _SHELL_HEADS | {"python", "python3", "node"}
            and "|" in command
        ):
            segments[index + 1] = Segment(
                segments[index + 1].text, heads[index + 1], "destructive", segments[index + 1].pattern
            )
    scan.segments = segments
    if reason:
        scan.opaque, scan.reason = True, reason
    if not segments and not scan.opaque:
        scan.opaque, scan.reason = True, "nothing to run"
    return scan
