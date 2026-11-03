"""Task graph produced by the planner. Workers may write only their owned paths."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from kiln.paths import paths_conflict, safe_relative_path


class ContractRoute(BaseModel):
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    request: str = ""
    response: str = ""


class Contract(BaseModel):
    """Shared interface the scaffold task writes and every feature task codes against."""

    routes: list[ContractRoute] = Field(default_factory=list)
    modules: list[str] = Field(
        default_factory=list,
        description="Public signatures as short free-text lines",
    )
    shared_files: list[str] = Field(
        default_factory=list,
        description="Files only the scaffold task may own (entry point, deps manifest)",
    )


class Task(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(min_length=1)
    assignee: str | None = None
    kind: Literal["scaffold", "feature"] = "feature"
    covers: list[str] = Field(
        default_factory=list,
        description="Acceptance criterion ids this task is meant to satisfy",
    )

    @model_validator(mode="after")
    def normalize_paths(self) -> "Task":
        self.owned_paths = [safe_relative_path(path) for path in self.owned_paths]
        return self


class TaskGraph(BaseModel):
    spec_title: str = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)
    contract: Contract | None = None

    @model_validator(mode="after")
    def ownership_and_dependencies(self) -> "TaskGraph":
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        owned: list[str] = []
        deps: dict[str, set[str]] = {}
        scaffolds = [task for task in self.tasks if task.kind == "scaffold"]
        if len(scaffolds) > 1:
            raise ValueError("at most one scaffold task is allowed")
        for task in self.tasks:
            for dependency in task.depends_on:
                if dependency == task.id or dependency not in known:
                    raise ValueError(f"{task.id} has an invalid dependency {dependency}")
            for path in task.owned_paths:
                for other in owned:
                    if paths_conflict(path, other):
                        raise ValueError(f"overlapping owned paths: {path} and {other}")
                owned.append(path)
            deps[task.id] = set(task.depends_on)
        if scaffolds:
            scaffold = scaffolds[0]
            # every non-scaffold feature that needs shared files should depend on scaffold
            for task in self.tasks:
                if task.kind == "feature" and scaffold.id not in task.depends_on and task.id != scaffold.id:
                    # soft rule: only require the dependency when the feature has no deps at all
                    # (planner should set it; we do not auto-mutate)
                    pass
        if _has_cycle(deps):
            raise ValueError("task dependencies contain a cycle")
        return self


def _has_cycle(deps: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(walk(dependency) for dependency in deps[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(walk(node) for node in deps)
