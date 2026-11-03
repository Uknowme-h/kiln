"""Stack profiles: the one place that knows how a stack is detected and run.

A profile answers, for one stack, the questions the team asks of the user's
machine: is the toolchain here and new enough, how would we install it on this
OS, how do dependencies get installed, which command runs the tests (writing
JUnit XML the test gate parses), and which fast checks catch a broken file
before the real tests run.

Commands are argv lists, never shell strings: they run without a shell, so
there is no quoting to get wrong on Windows, macOS, or Linux, and the approval
prompt shows exactly what executes. Install commands live here, on the
companion side, so a server can never smuggle its own command into an install.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

RESULTS_FILE = "kiln-results.xml"
VENV_DIR = ".venv"

_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, ...] | None:
    """The first dotted version in the text, e.g. 'Python 3.12.4' -> (3, 12, 4)."""
    match = _VERSION.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


@dataclass(frozen=True)
class ToolNeed:
    """One tool a stack needs on the machine."""

    key: str  # also the name looked up in the install table
    label: str
    min_version: tuple[int, ...]
    probes_default: tuple[tuple[str, ...], ...]
    probes_windows: tuple[tuple[str, ...], ...] = ()
    # npm ships with node, so it is never installed on its own
    installed_with: str = ""

    def probes(self, os_name: str) -> tuple[tuple[str, ...], ...]:
        if os_name == "Windows" and self.probes_windows:
            return self.probes_windows
        return self.probes_default


# manager -> tool key -> argv
INSTALLERS: dict[str, dict[str, tuple[str, ...]]] = {
    "winget": {
        "python": (
            "winget", "install", "-e", "--id", "Python.Python.3.12",
            "--accept-package-agreements", "--accept-source-agreements",
        ),
        "node": (
            "winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS",
            "--accept-package-agreements", "--accept-source-agreements",
        ),
    },  # fmt: skip
    "choco": {
        "python": ("choco", "install", "-y", "python"),
        "node": ("choco", "install", "-y", "nodejs-lts"),
    },
    "scoop": {
        "python": ("scoop", "install", "python"),
        "node": ("scoop", "install", "nodejs-lts"),
    },
    "brew": {
        "python": ("brew", "install", "python@3.12"),
        "node": ("brew", "install", "node"),
    },
    "apt-get": {
        "python": ("sudo", "apt-get", "install", "-y", "python3", "python3-venv", "python3-pip"),
        "node": ("sudo", "apt-get", "install", "-y", "nodejs", "npm"),
    },
    "dnf": {
        "python": ("sudo", "dnf", "install", "-y", "python3", "python3-pip"),
        "node": ("sudo", "dnf", "install", "-y", "nodejs", "npm"),
    },
    "pacman": {
        "python": ("sudo", "pacman", "-S", "--noconfirm", "python", "python-pip"),
        "node": ("sudo", "pacman", "-S", "--noconfirm", "nodejs", "npm"),
    },
}
# which package manager to reach for first, per OS
MANAGER_ORDER: dict[str, tuple[str, ...]] = {
    "Windows": ("winget", "choco", "scoop"),
    "macOS": ("brew",),
    "Linux": ("apt-get", "dnf", "pacman"),
}


def install_command(tool_key: str, os_name: str, tools: Mapping[str, str | None]) -> list[str] | None:
    """The install argv for this tool on this machine, or None if no manager is available."""
    for manager in MANAGER_ORDER.get(os_name, ()):
        if tools.get(manager) and tool_key in INSTALLERS[manager]:
            return list(INSTALLERS[manager][tool_key])
    return None


def venv_python(workspace: Path, os_name: str) -> Path:
    sub = ("Scripts", "python.exe") if os_name == "Windows" else ("bin", "python")
    return workspace / VENV_DIR / sub[0] / sub[1]


@dataclass(frozen=True)
class StackProfile:
    id: str
    label: str
    needs: tuple[ToolNeed, ...]
    test_extensions: tuple[str, ...]
    # extra prompt file appended for the test author (None: the base prompt fits)
    test_prompt: str | None
    # what the developers are told about running things in this stack
    dev_notes: str

    def install_deps_commands(
        self, workspace: Path, os_name: str, python_argv: list[str] | None = None
    ) -> list[list[str]]:
        if self.id == "python":
            commands: list[list[str]] = []
            venv_py = venv_python(workspace, os_name)
            if not venv_py.exists():
                commands.append([*(python_argv or ["python"]), "-m", "venv", VENV_DIR])
            pip = [str(venv_py), "-m", "pip", "install", "-q", "pytest"]
            if (workspace / "requirements.txt").is_file():
                pip += ["-r", "requirements.txt"]
            commands.append(pip)
            return commands
        if self.id == "node":
            return [["npm", "install"]] if (workspace / "package.json").is_file() else []
        return []

    def test_command(self, workspace: Path, os_name: str, python_argv: list[str] | None = None) -> list[str]:
        if self.id == "python":
            venv_py = venv_python(workspace, os_name)
            python = [str(venv_py)] if venv_py.exists() else (python_argv or ["python"])
            return [*python, "-m", "pytest", "-q", f"--junitxml={RESULTS_FILE}"]
        if self.id == "node":
            return [
                "npx", "--no-install", "vitest", "run",
                "--reporter=junit", f"--outputFile={RESULTS_FILE}",
            ]  # fmt: skip
        raise ValueError(f"no test command for stack {self.id}")

    def smoke_commands(
        self,
        workspace: Path,
        paths: list[str],
        os_name: str,
        python_argv: list[str] | None = None,
    ) -> list[list[str]]:
        """Fast checks that catch a broken file before the real tests run."""
        existing = [p for p in paths if (workspace / p).is_file()]
        if self.id == "python":
            files = [p for p in existing if p.endswith(".py")]
            if not files:
                return []
            venv_py = venv_python(workspace, os_name)
            python = [str(venv_py)] if venv_py.exists() else (python_argv or ["python"])
            return [[*python, "-m", "compileall", "-q", *files]]
        if self.id == "node":
            commands = [
                ["node", "--check", p] for p in existing if p.endswith((".js", ".mjs", ".cjs"))
            ]
            # tsc needs node_modules to resolve anything; before deps are
            # installed the type check cannot run meaningfully, so skip it
            # rather than report a failure that is really a missing install
            if (
                (workspace / "node_modules").is_dir()
                and (workspace / "tsconfig.json").is_file()
                and any(p.endswith((".ts", ".tsx")) for p in existing)
            ):
                commands.append(["npx", "--no-install", "tsc", "--noEmit"])
            return commands
        return []


def _python_notes() -> str:
    return (
        "Stack: Python. The team creates a virtual environment in `.venv` inside the "
        "workspace and installs `requirements.txt` plus pytest into it. Run Python and "
        "pytest through that interpreter (`.venv\\Scripts\\python.exe` on Windows, "
        "`.venv/bin/python` elsewhere), never the system one. List every runtime "
        "dependency in `requirements.txt`."
    )


def _node_notes() -> str:
    return (
        "Stack: Node.js. Dependencies are installed with `npm install` from "
        "`package.json`. Tests use vitest: put `vitest` in devDependencies and expose "
        "`npm test` as `vitest run`. Use ES modules or CommonJS consistently. List every "
        "dependency in `package.json`."
    )


PROFILES: dict[str, StackProfile] = {
    "python": StackProfile(
        id="python",
        label="Python (pytest)",
        needs=(
            ToolNeed(
                key="python",
                label="Python 3",
                min_version=(3, 10),
                probes_default=(("python3", "--version"), ("python", "--version")),
                probes_windows=(
                    ("py", "-3", "--version"),
                    ("python", "--version"),
                    ("python3", "--version"),
                ),
            ),
        ),
        test_extensions=(".py",),
        test_prompt=None,
        dev_notes=_python_notes(),
    ),
    "node": StackProfile(
        id="node",
        label="Node.js / TypeScript (vitest)",
        needs=(
            ToolNeed(
                key="node",
                label="Node.js 20+",
                min_version=(20, 0),
                probes_default=(("node", "--version"),),
            ),
            ToolNeed(
                key="npm",
                label="npm",
                min_version=(9, 0),
                probes_default=(("npm", "--version"),),
                installed_with="node",
            ),
        ),
        test_extensions=(".js", ".mjs", ".cjs", ".ts", ".mts"),
        test_prompt="test_author_node",
        dev_notes=_node_notes(),
    ),
}
STACK_IDS = tuple(PROFILES)


def profile_for(stack_id: str) -> StackProfile:
    try:
        return PROFILES[stack_id]
    except KeyError:
        raise ValueError(f"unknown stack {stack_id!r}; known: {', '.join(STACK_IDS)}") from None
