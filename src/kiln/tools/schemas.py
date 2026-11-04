"""Tool inputs the agents may call. Validation happens on both ends of the relay.

The server validates what the model produced before relaying it; the
companion validates again on arrival (never trust the wire). Nothing in this
module executes anything.
"""

from typing import Literal

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
    include: str | None = Field(
        default=None,
        description="Only files whose path or name matches this glob, e.g. *.tsx or page.tsx",
    )

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
    # Set by Kiln for mid-build questions: after this long the questions count
    # as skipped, so an absent user never holds the prompt (and with it every
    # command approval) forever. None waits for an answer.
    timeout_seconds: int | None = Field(default=None, ge=30, le=3600)


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


class NotifyPeer(BaseModel):
    """Send a short note to your teammate developer."""

    message: str = Field(
        min_length=1,
        max_length=1200,
        description="What changed or what you need: name the file, route, export or id",
    )
    reply_to: str | None = Field(
        default=None, description="Question id (e.g. Q3) when this message answers a question"
    )


class AskPeer(BaseModel):
    """Ask your teammate a question only they can answer."""

    question: str = Field(min_length=1, max_length=600)
    wait: bool = Field(
        default=False,
        description=(
            "false = keep working, the answer arrives as a message; true = pause "
            "until they answer (only when nothing else can move)"
        ),
    )


class Escalate(BaseModel):
    """Bring a problem to the tech lead."""

    kind: Literal["product", "environment", "plan", "blocked"] = Field(
        description=(
            "product = a product decision nobody recorded; environment = the machine or "
            "toolchain is in the way; plan = your task is too big, missing a dependency or "
            "a helper task; blocked = anything else stopping you"
        )
    )
    summary: str = Field(min_length=1, max_length=400, description="The problem in one sentence")
    details: str = Field(
        default="", max_length=4000,
        description="What you tried, the exact error or output, the options you see",
    )
    wait: bool = Field(
        default=True,
        description="true = pause until the lead decides; false = keep working, the decision arrives as a message",
    )


class ContractField(BaseModel):
    name: str = Field(min_length=1, description="Exact JSON key")
    type: str = Field(min_length=1, description="string | integer | number | boolean | object | <type>[]")
    required: bool = True
    description: str = ""


class ProposeContractChange(BaseModel):
    """Change (or add) one api route of the shared contract."""

    method: str = Field(min_length=1, description="HTTP method of the route")
    path: str = Field(min_length=1, description="Route path exactly as in the contract")
    request_fields: list[ContractField] | None = Field(
        default=None, description="The full new request field list, or null to keep it"
    )
    response_fields: list[ContractField] | None = Field(
        default=None, description="The full new response field list, or null to keep it"
    )
    status: int | None = Field(default=None, description="New success status, or null to keep it")
    reason: str = Field(min_length=1, max_length=400, description="Why the contract must change")


class RequestFiles(BaseModel):
    """Ask to edit files outside your task's owned paths."""

    paths: list[str] = Field(
        min_length=1, max_length=8, description="Workspace-relative files you need to edit"
    )
    reason: str = Field(
        min_length=1, max_length=300, description="What you will change and why"
    )
    wait: bool = Field(
        default=False,
        description=(
            "If a teammate holds a file: false = keep working on your other files and "
            "get a message when it is yours; true = pause until it is released "
            "(only when nothing else in your task can move)"
        ),
    )


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
