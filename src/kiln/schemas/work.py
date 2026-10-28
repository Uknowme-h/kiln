"""What a worker returns for its task: files, what works, known gaps.

The sources / conclusions / unresolved-gaps contract: the fixer reads gaps
instead of re-deriving them, the reporter aggregates them, and the planner
sees honest partial state when a task escalates. Files may only touch the
task's owned paths — enforced when they are supplied via pydantic context.
"""

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from kiln.paths import safe_relative_path


class WorkFile(BaseModel):
    path: str = Field(min_length=1)
    content: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def workspace_relative(cls, value: str) -> str:
        return safe_relative_path(value)


class WorkerResult(BaseModel):
    # pytest must not mistake the model for a test class
    __test__ = False

    task_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    files: list[WorkFile] = Field(min_length=1)
    what_works: str = Field(min_length=1)
    known_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def only_the_task_and_its_paths(self, info: ValidationInfo) -> "WorkerResult":
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")

        context = info.context or {}
        task_id = context.get("task_id")
        if task_id is not None and self.task_id != task_id:
            raise ValueError(f"result is for {self.task_id}, expected {task_id}")

        owned = context.get("owned_paths")
        if owned is not None:
            prefixes = tuple(owned)
            for path in paths:
                if path.startswith("tests/"):
                    raise ValueError("workers never write test files")
                if not any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes):
                    raise ValueError(f"{path} is outside the task's owned paths")
        return self
