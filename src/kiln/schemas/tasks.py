"""Task graph produced by the planner. Workers may write only their owned paths."""

from pydantic import BaseModel, Field, model_validator

from kiln.paths import paths_conflict, safe_relative_path


class Task(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(min_length=1)
    assignee: str | None = None

    @model_validator(mode="after")
    def normalize_paths(self) -> "Task":
        self.owned_paths = [safe_relative_path(path) for path in self.owned_paths]
        return self


class TaskGraph(BaseModel):
    spec_title: str = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def ownership_and_dependencies(self) -> "TaskGraph":
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        owned: list[str] = []
        deps: dict[str, set[str]] = {}
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
