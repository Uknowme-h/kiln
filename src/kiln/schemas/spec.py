"""Typed spec produced by the requirements analyst."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from kiln.paths import safe_relative_path
from kiln.stacks.profiles import STACK_IDS

StackId = Literal["python", "node"]

# Frameworks are a separate axis from stack_id: stack_id picks the test
# toolchain (pytest vs vitest), frameworks pick the scaffold recipe and the
# framework skill pack. Recipes live in kiln.stacks.scaffold_recipes.
FrameworkId = Literal[
    "fastapi",
    "flask",
    "django",
    "express",
    "nestjs",
    "vite_react",
    "nextjs",
]
FRAMEWORK_IDS: tuple[str, ...] = (
    "fastapi",
    "flask",
    "django",
    "express",
    "nestjs",
    "vite_react",
    "nextjs",
)

API_FRAMEWORKS = frozenset({"fastapi", "flask", "django", "express", "nestjs"})
UI_FRAMEWORKS = frozenset({"vite_react", "nextjs"})


class WorkspaceRoot(BaseModel):
    """One independently-scaffolded tree. ``path`` "" or "." means single-root."""

    path: str = Field(
        default=".",
        description="Workspace-relative root, e.g. 'backend' or 'frontend'; '.' = single-root",
    )
    role: Literal["api", "web", "fullstack"]
    framework: FrameworkId

    @model_validator(mode="after")
    def normalize_path(self) -> "WorkspaceRoot":
        path = (self.path or "").strip() or "."
        self.path = "." if path == "." else safe_relative_path(path)
        return self


class UserStory(BaseModel):
    id: str = Field(min_length=1)
    as_a: str = Field(min_length=1)
    i_want: str = Field(min_length=1)
    so_that: str = Field(min_length=1)


class AcceptanceCriterion(BaseModel):
    id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    given: str = Field(min_length=1)
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)
    oracle: str = Field(
        min_length=1,
        description=(
            "The exact automated check that proves this criterion, "
            "e.g. the test file and what it asserts."
        ),
    )


class FilePlanEntry(BaseModel):
    path: str = Field(min_length=1)
    purpose: str = Field(min_length=1)


class Spec(BaseModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    stack: str = Field(min_length=1, description="Human-readable stack description")
    stack_id: StackId = Field(
        default="python",
        description=(
            f"Machine stack id; one of {', '.join(STACK_IDS)}. This is the "
            "language of the acceptance-oracle test suite the team writes "
            "(python -> pytest, node -> vitest) — NOT the set of frameworks. "
            "A Next.js frontend plus any python API must use stack_id 'node' "
            "with the API checked through node-side HTTP contract tests, "
            "because the suite runs in one language only."
        ),
    )
    frameworks: list[FrameworkId] = Field(
        default_factory=list,
        description=(
            f"Frameworks the product is built on; any of {', '.join(FRAMEWORK_IDS)}. "
            "Drives deterministic scaffolding and skill packs. Empty = no framework."
        ),
    )
    roots: list[WorkspaceRoot] = Field(
        default_factory=list,
        description=(
            "Independently-scaffolded trees. Empty or a single '.' root = single-root "
            "app. An API plus a Vite SPA must split into 'backend' + 'frontend'; "
            "nextjs alone may own the whole stack as one root."
        ),
    )
    user_stories: list[UserStory] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    file_plan: list[FilePlanEntry] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def story_references_resolve(self) -> "Spec":
        story_ids = [story.id for story in self.user_stories]
        if len(story_ids) != len(set(story_ids)):
            raise ValueError("user story ids must be unique")
        known = set(story_ids)
        criterion_ids = [item.id for item in self.acceptance_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("acceptance criterion ids must be unique")
        for criterion in self.acceptance_criteria:
            if criterion.story_id not in known:
                raise ValueError(f"unknown story_id {criterion.story_id}")
        self._validate_frameworks_and_roots()
        return self

    def _validate_frameworks_and_roots(self) -> None:
        frameworks = list(dict.fromkeys(self.frameworks))  # unique, order kept
        if len(frameworks) != len(self.frameworks):
            raise ValueError("frameworks must not contain duplicates")
        if not self.roots:
            return
        # every root points at a declared framework; paths are unique and disjoint
        seen_paths: list[str] = []
        for root in self.roots:
            if root.framework not in frameworks:
                raise ValueError(
                    f"root {root.path!r} uses framework {root.framework!r} "
                    "that is not in spec.frameworks"
                )
            for other in seen_paths:
                if other != "." and root.path != "." and (
                    root.path == other
                    or root.path.startswith(other + "/")
                    or other.startswith(root.path + "/")
                ):
                    raise ValueError(f"overlapping roots: {root.path!r} and {other!r}")
            seen_paths.append(root.path)
        has_api = any(f in API_FRAMEWORKS for f in frameworks)
        has_spa = "vite_react" in frameworks
        has_next = "nextjs" in frameworks
        # An API plus a Vite SPA must split into separate roots; nextjs alone
        # may own the whole stack as one root.
        if has_api and has_spa and len(self.roots) < 2:
            raise ValueError(
                "an API framework plus vite_react needs split roots "
                "(e.g. 'backend' + 'frontend')"
            )
        # nextjs owns the full stack; pairing it with a python API means the
        # suite is node, so the API is verified through node contract tests.
        if has_next and has_api and self.stack_id != "node":
            raise ValueError(
                "nextjs plus an API framework must use stack_id 'node' "
                "(the API is checked via node-side contract tests)"
            )
