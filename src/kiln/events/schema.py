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


FloorEvent = Annotated[
    RunStarted
    | RunFinished
    | AgentStatus
    | TaskAssigned
    | MessageSent
    | TestRunFinished
    | UsageTick,
    Field(discriminator="kind"),
]


def encode_event(event: BaseModel) -> str:
    return event.model_dump_json()


def decode_event(payload: str) -> BaseModel:
    from pydantic import TypeAdapter

    return TypeAdapter(FloorEvent).validate_json(payload)
