"""The tech lead's structured decision.

One escalation in, one decision out. The orchestrator validates and executes
it: a user question goes through the companion, a plan amendment is rebuilt
into a TaskGraph and validated like the original plan, an install runs the
existing installer with the user's approval.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from kiln.schemas.tasks import Task

EscalationKind = Literal["product", "environment", "plan", "blocked"]


class UserQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=400)
    options: list[str] = Field(min_length=2, max_length=5)
    recommended: str = Field(min_length=1, description="One of options; used if the user does not answer")

    @model_validator(mode="after")
    def recommended_is_an_option(self) -> "UserQuestion":
        if self.recommended not in self.options:
            raise ValueError("recommended must be one of the options")
        return self


class DependencyChange(BaseModel):
    task_id: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list, description="The task's full new dependency list")


class Reassignment(BaseModel):
    task_id: str = Field(min_length=1)
    developer: Literal["A", "B"]


class PlanAmendment(BaseModel):
    add_tasks: list[Task] = Field(default_factory=list)
    replace_task: str | None = Field(
        default=None,
        description="A pending task the added tasks replace (a split); its dependents follow them",
    )
    dependencies: list[DependencyChange] = Field(default_factory=list)
    reassign: list[Reassignment] = Field(default_factory=list)


class Decision(BaseModel):
    action: Literal["answer", "ask_user", "amend_plan", "install", "proceed"]
    message: str = Field(min_length=1, max_length=1500, description="What the developer should do next")
    question: UserQuestion | None = None
    amendment: PlanAmendment | None = None
    install: str | None = Field(default=None, description="Tool key from the stack probe")

    @model_validator(mode="after")
    def payload_matches_action(self) -> "Decision":
        if self.action == "ask_user" and self.question is None:
            raise ValueError("ask_user needs a question")
        if self.action == "amend_plan" and self.amendment is None:
            raise ValueError("amend_plan needs an amendment")
        if self.action == "install" and not self.install:
            raise ValueError("install needs a tool key")
        return self


class Escalation(BaseModel):
    kind: EscalationKind
    summary: str = Field(min_length=1, max_length=400)
    details: str = Field(default="", max_length=4000)
    source: str = Field(default="", description="Who raised it: an agent id, or 'kiln' for automatic triggers")
    task_id: str = ""
