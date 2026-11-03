"""Tool inputs the agents may call. Validation happens on both ends of the relay.

The server validates what the model produced before relaying it; the
companion validates again on arrival (never trust the wire). Nothing in this
module executes anything.
"""

from pydantic import BaseModel, Field, field_validator

from kiln.paths import safe_relative_path


def _dir_path(value: str) -> str:
    """A directory inside the workspace; '.' means the workspace root."""
    if value in ("", "."):
        return "."
    return safe_relative_path(value)


class ReadFile(BaseModel):
    path: str
    offset: int | None = Field(default=None, ge=1, description="First line to return (1-based)")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines")

    @field_validator("path")
    @classmethod
    def relative(cls, value: str) -> str:
        return safe_relative_path(value)


class WriteFile(BaseModel):
    path: str
    content: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def relative(cls, value: str) -> str:
        return safe_relative_path(value)


class EditFile(BaseModel):
    path: str
    old_string: str = Field(min_length=1, description="Exact text to replace")
    new_string: str = Field(description="Replacement text; must differ from old_string")
    replace_all: bool = False

    @field_validator("path")
    @classmethod
    def relative(cls, value: str) -> str:
        return safe_relative_path(value)


class ListDir(BaseModel):
    path: str = "."

    @field_validator("path")
    @classmethod
    def inside(cls, value: str) -> str:
        return _dir_path(value)


class Glob(BaseModel):
    pattern: str = Field(min_length=1, description="Glob such as src/**/*.py")
    path: str = "."

    @field_validator("path")
    @classmethod
    def inside(cls, value: str) -> str:
        return _dir_path(value)


class Grep(BaseModel):
    pattern: str = Field(min_length=1, description="Regular expression")
    path: str = "."
    include: str | None = Field(default=None, description="Only files matching this glob")

    @field_validator("path")
    @classmethod
    def inside(cls, value: str) -> str:
        return _dir_path(value)


class ShellCommand(BaseModel):
    """A command line. The companion shows it to the user and runs it only on approval."""

    command: str = Field(min_length=1)


class Shell(BaseModel):
    command: str = Field(min_length=1, description="Shell command string to execute")
    workdir: str | None = Field(
        default=None, description="Folder inside the workspace to run in (default: its root)"
    )
    timeout_ms: int | None = Field(
        default=None, ge=1000, le=900_000, description="Timeout in milliseconds (default 120000)"
    )
    background: bool = Field(
        default=False, description="Start a long-running process (dev server) and return at once"
    )

    @field_validator("workdir")
    @classmethod
    def inside(cls, value: str | None) -> str | None:
        return None if value is None else _dir_path(value)


class Question(BaseModel):
    header: str = Field(min_length=1, max_length=40, description="A short label")
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, description="Suggested answers")
    multiple: bool = Field(default=False, description="Allow more than one option")


class AskUser(BaseModel):
    questions: list[Question] = Field(min_length=1, max_length=5)


class WebSearch(BaseModel):
    query: str = Field(min_length=1)


class WebFetch(BaseModel):
    url: str = Field(min_length=1, pattern=r"^https?://")


class ListPeers(BaseModel):
    """No input: shows the other developers' tasks, owners, status, and files so far."""


class SubmitDev(BaseModel):
    """The developer's final report. The files themselves are already on disk."""

    task_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    files_written: list[str] = Field(default_factory=list)
    what_works: str = Field(min_length=1)
    known_gaps: list[str] = Field(default_factory=list)


class SubmitFix(BaseModel):
    """The fixer's final report. The corrected files are already on disk."""

    diagnosis: str = Field(min_length=1)
    files_written: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    """Load a built-in Kiln skill by name (local; never hits the workspace)."""

    name: str = Field(min_length=1, description="Skill id, e.g. ui_product, prod_ops, security")


class SpawnExplore(BaseModel):
    """Spawn a read-only explore subagent; returns a bounded summary."""

    prompt: str = Field(min_length=1, description="What to research or find")


class SubmitExplore(BaseModel):
    """Explore child's finish payload."""

    summary: str = Field(min_length=1)
    paths_touched: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class TodoItem(BaseModel):
    id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    status: str = Field(default="pending", description="pending | in_progress | done")


class TodoWrite(BaseModel):
    """Replace or merge the in-loop checklist for this agent."""

    items: list[TodoItem] = Field(min_length=1, max_length=12)
    merge: bool = Field(
        default=True,
        description="If true, update matching ids and append new ones; else replace all",
    )


class TodoRead(BaseModel):
    """No input: return the current checklist."""


class Diagnostics(BaseModel):
    """Run stack diagnostics on owned paths (syntax/type smoke)."""

    paths: list[str] = Field(default_factory=list, description="Relative paths to check")
