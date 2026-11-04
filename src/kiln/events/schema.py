"""Typed event contract for the live office view.

Every floor event the UI can render is one of the models in ``FloorEvent``.
The UI never reads run state directly; it only consumes this stream, so the
agent loop and the viewer stay decoupled. Serialisation is JSON over SSE.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from kiln.agents.roles import Role


class AgentRef(BaseModel):
    """One seat on the floor. Workers get a per-run id (``worker-1`` ...)."""

    id: str = Field(min_length=1)
    role: Role
    label: str = Field(min_length=1)


class TaskRef(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    owned_paths: list[str] = Field(default_factory=list)


class RunStarted(BaseModel):
    kind: Literal["run_started"] = "run_started"
    run_id: str = Field(min_length=1)
    idea: str = Field(min_length=1)


class RunFinished(BaseModel):
    kind: Literal["run_finished"] = "run_finished"
    run_id: str = Field(min_length=1)
    green: bool
    wall_clock_seconds: float = Field(ge=0)
    # what went wrong during the build and how the team recovered (failed
    # tasks, repairs, contract changes, the lead's decisions) — the Handoff
    notes: list[str] = Field(default_factory=list)


class AgentStatus(BaseModel):
    """Drives the avatar pose. ``working`` means a model call or a tool call."""

    kind: Literal["agent_status"] = "agent_status"
    agent: AgentRef
    status: Literal["idle", "thinking", "working", "stuck", "done"]
    detail: str = ""


class TaskAssigned(BaseModel):
    kind: Literal["task_assigned"] = "task_assigned"
    task: TaskRef
    agent_id: str = Field(min_length=1)


class MessageSent(BaseModel):
    """Rendered as an envelope flying from one desk to another."""

    kind: Literal["message_sent"] = "message_sent"
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class TestRunFinished(BaseModel):
    kind: Literal["test_run_finished"] = "test_run_finished"
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    errors: int = Field(ge=0)
    green: bool


class UsageTick(BaseModel):
    kind: Literal["usage_tick"] = "usage_tick"
    tokens_used: int = Field(ge=0)
    ceiling: int = Field(gt=0)
    calls: int = Field(ge=0)


class NoteAdded(BaseModel):
    """A mid-run note from the user, delivered to the planner's mailbox.

    Rendered as an envelope from the client landing on the cork board.
    """

    kind: Literal["note_added"] = "note_added"
    text: str = Field(min_length=1)


class FloorQuestion(BaseModel):
    header: str = ""
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    multiple: bool = False


class ApprovalPending(BaseModel):
    """An agent's command or ask_user questions waiting for the user's answer.

    The floor renders the same dialog the companion terminal shows — the exact
    lines under ``lines``, or selectable ``questions`` for ask_user — and the
    user may answer in either place; the reply is relayed to the companion.
    """

    kind: Literal["approval_pending"] = "approval_pending"
    agent_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    summary: str = ""
    lines: list[str] = Field(default_factory=list)
    note: str = ""
    allow_run_scope: bool = False
    # auto-approve resolved it already: log it, do not pop a dialog
    auto_approved: bool = False
    questions: list[FloorQuestion] = Field(default_factory=list)


class ApprovalResolved(BaseModel):
    kind: Literal["approval_resolved"] = "approval_resolved"
    agent_id: str = Field(min_length=1)
    approved: bool
    auto_approved: bool = False


class TranscriptTick(BaseModel):
    """A transcript entry was recorded for an agent. Lightweight: the full
    entry is fetched over REST, never broadcast — prompts are too big for SSE."""

    kind: Literal["transcript_tick"] = "transcript_tick"
    agent_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    summary: str = ""


TeamCategory = Literal[
    "note", "question", "answer", "checkout", "contract", "status", "escalation", "decision"
]


class TeamEvent(BaseModel):
    """Something the team said to each other: rendered as an envelope between
    desks, a line in the event log and the team feed."""

    kind: Literal["team_event"] = "team_event"
    category: TeamCategory
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    text: str = Field(min_length=1)


class TeamLane(BaseModel):
    agent_id: str
    label: str
    focus: str = "shared"
    current: str | None = None
    current_title: str = ""
    checked_out: list[str] = Field(default_factory=list)
    paused: bool = False


class TeamTask(BaseModel):
    id: str
    title: str
    agent_id: str
    status: Literal["pending", "running", "done", "failed"]


class TeamUpdate(BaseModel):
    """A snapshot of the team board for the Team panel. Small by design."""

    kind: Literal["team_update"] = "team_update"
    lanes: list[TeamLane] = Field(default_factory=list)
    tasks: list[TeamTask] = Field(default_factory=list)
    open_questions: int = 0
    contract_changes: int = 0
    decisions: int = 0


FloorEvent = Annotated[
    RunStarted
    | RunFinished
    | AgentStatus
    | TaskAssigned
    | MessageSent
    | TestRunFinished
    | UsageTick
    | NoteAdded
    | ApprovalPending
    | ApprovalResolved
    | TranscriptTick
    | TeamEvent
    | TeamUpdate,
    Field(discriminator="kind"),
]


def encode_event(event: BaseModel) -> str:
    return event.model_dump_json()


def decode_event(payload: str) -> BaseModel:
    from pydantic import TypeAdapter

    return TypeAdapter(FloorEvent).validate_json(payload)
