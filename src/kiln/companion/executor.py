"""Tool execution inside one user-chosen directory.

This is the only code that touches the user's disk. Every path is validated
by `safe_relative_path` and then re-resolved under the workspace root as a
second line of defense. Shell commands never run on the host: `run_tests`
executes inside the Docker sandbox exactly as the server-side runner did.
"""

from pathlib import Path

from kiln.config import Settings
from kiln.paths import safe_relative_path
from kiln.sandbox.runner import run_tests
from kiln.tools.schemas import ReadFile, WriteFile


class ExecutionError(RuntimeError):
    """A tool input was invalid or the execution failed."""


class Executor:
    def __init__(self, workspace: Path, settings: Settings | None = None):
        self.root = workspace.resolve()
        if not self.root.is_dir():
            raise ExecutionError(f"workspace does not exist: {workspace}")
        self.settings = settings or Settings(_env_file=None)

    def _resolve(self, raw_path: str) -> Path:
        relative = safe_relative_path(raw_path)
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ExecutionError(f"path escapes the workspace: {raw_path}")
        return candidate

    def read_file(self, raw_path: str) -> str:
        path = self._resolve(ReadFile(path=raw_path).path)
        if not path.is_file():
            raise ExecutionError(f"no such file in the workspace: {raw_path}")
        return path.read_text(encoding="utf-8")

    def write_file(self, raw_path: str, content: str) -> str:
        validated = WriteFile(path=raw_path, content=content)
        path = self._resolve(validated.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(validated.content, encoding="utf-8")
        return f"wrote {validated.path} ({len(validated.content)} bytes)"

    def run_tests(self) -> str:
        report = run_tests(self.root, self.settings)
        return report.model_dump_json()

    def execute(self, tool: str, tool_input: dict) -> str:
        if tool == "read_file":
            return self.read_file(str(tool_input.get("path", "")))
        if tool == "write_file":
            return self.write_file(
                str(tool_input.get("path", "")),
                str(tool_input.get("content", "")),
            )
        if tool == "run_tests":
            return self.run_tests()
        raise ExecutionError(f"unknown tool: {tool}")
