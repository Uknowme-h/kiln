"""Wire messages between the server and a companion.

Deliberately small: hello, tool_call, tool_result, progress, ping/pong.
Hello may omit a workspace (listen mode); the UI then locks one via the
control tools browse_host / set_workspace. Agent tool inputs are the
validated models from kiln.tools. A tool call names the agent making it and
the paths that agent owns, so the companion can enforce ownership and show
the user who is asking.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

TOOLS = (
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "write_file",
    "edit_file",
    "shell",
    "ask_user",
    "web_fetch",
    "web_search",
    "probe_stack",
    "install_tool",
    "install_deps",
    "run_tests",
    "smoke_check",
    "served_ui_audit",
    "runtime_smoke",
    "claim_scope",
    "release_scope",
    "vendor_deps",  # only meaningful for the optional Docker sandbox
)

# Relayed like tools but reserved for the listen → UI folder-pick handshake.
# Not part of the agent TOOLS list; only the server/UI may call them.
CONTROL_TOOLS = frozenset({"browse_host", "set_workspace"})


class Hello(BaseModel):
    """First message from the companion: claims a pair code and describes the machine.

    ``workspace`` may be empty when the companion is in listen mode — the UI
    picks the folder after pairing via browse_host / set_workspace.
    """

    type: Literal["hello"] = "hello"
    code: str = Field(min_length=1)
    workspace: str = ""
    version: str = Field(min_length=1)
    # OS, shell, architecture, and the runtimes found on PATH (see companion.shellenv)
    environment: dict = Field(default_factory=dict)


class HelloAck(BaseModel):
    type: Literal["hello_ack"] = "hello_ack"
    run_id: str = Field(min_length=1)


class AgentScope(BaseModel):
    """Who is calling and what they may write. Server-supplied, re-checked on arrival."""

    id: str = Field(min_length=1)
    label: str = ""
    role: str = ""
    owned_paths: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    input: dict
    agent: AgentScope | None = None


class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    id: str = Field(min_length=1)
    ok: bool
    output: str = ""
    error: str = ""
    # "" success or ordinary failure, "denied" when policy or the user refused
    code: str = ""


class QuestionPayload(BaseModel):
    """One ask_user question mirrored to the web floor."""

    header: str = ""
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    multiple: bool = False


class ToolProgress(BaseModel):
    """A call is still alive. Resets the server's inactivity timer, and tells the
    floor when a command is waiting on the user's answer."""

    type: Literal["progress"] = "progress"
    id: str = Field(min_length=1)
    state: Literal["working", "awaiting_approval", "approval_resolved"] = "working"
    detail: str = ""
    agent: str = ""
    action: str = ""
    approved: bool | None = None
    # what is being approved, so the web floor can render the same dialog the
    # terminal shows; empty for heartbeats and plain working updates
    lines: list[str] = Field(default_factory=list)
    note: str = ""
    allow_run_scope: bool = False
    # ask_user: structured questions for the floor dialog (chips + free text)
    questions: list[QuestionPayload] = Field(default_factory=list)


class ApprovalAnswer(BaseModel):
    """The web floor's reply to a pending approval, relayed server → companion.

    The terminal and the floor race: whichever answer the user gives first
    resolves the prompt, and the other channel is told it was too late.
    For ask_user, ``answers`` carries one list of strings per question."""

    type: Literal["approval_answer"] = "approval_answer"
    approved: bool = True
    scope: str = "once"  # "once" or "run"; coerced down if the request wasn't low-risk
    feedback: str = ""
    answers: list[list[str]] | None = None


class Ping(BaseModel):
    type: Literal["ping"] = "ping"


class Pong(BaseModel):
    type: Literal["pong"] = "pong"


ServerMessage = Annotated[HelloAck | ToolCall | ApprovalAnswer | Ping, Field(discriminator="type")]
CompanionMessage = Annotated[
    Hello | ToolResult | ToolProgress | Pong, Field(discriminator="type")
]


def decode_companion_message(payload: str) -> Hello | ToolResult | ToolProgress | Pong:
    from pydantic import TypeAdapter

    return TypeAdapter(CompanionMessage).validate_json(payload)
