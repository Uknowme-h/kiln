"""Task graph produced by the planner. Workers may write only their owned paths."""

from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from kiln.paths import paths_conflict, safe_relative_path


BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class FieldSpec(BaseModel):
    """One field of a request or response body — the unit both developers code against."""

    name: str = Field(min_length=1, description="Exact JSON key, e.g. 'email'")
    type: str = Field(
        min_length=1,
        description="string | integer | number | boolean | object | <type>[] | date-time",
    )
    required: bool = True
    description: str = ""


def _duplicate_names(fields: list[FieldSpec]) -> list[str]:
    seen: set[str] = set()
    return [f.name for f in fields if f.name in seen or seen.add(f.name)]


class ContractRoute(BaseModel):
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    # short human summaries, kept for older plans; the field lists are the contract
    request: str = ""
    response: str = ""
    kind: Literal["api", "page"] = Field(
        default="api", description="api = JSON endpoint; page = a served UI page"
    )
    request_fields: list[FieldSpec] = Field(
        default_factory=list, description="Body fields the client sends (POST/PUT/PATCH)"
    )
    response_fields: list[FieldSpec] = Field(
        default_factory=list, description="Fields of the success response body"
    )
    status: int = Field(default=200, ge=100, le=599, description="Success status code")
    errors: list[int] = Field(default_factory=list, description="Error status codes it may return")
    auth: bool = Field(default=False, description="Requires a signed-in user")

    def vagueness(self) -> list[str]:
        """Why this route is too vague for two developers to build against."""
        if self.kind == "page":
            return []
        problems = []
        name = f"{self.method.upper()} {self.path}"
        if self.method.upper() in BODY_METHODS and not self.request_fields:
            problems.append(f"{name} has no request_fields")
        if self.status != 204 and not self.response_fields:
            problems.append(f"{name} has no response_fields (use status 204 for no body)")
        for label, fields in (("request", self.request_fields), ("response", self.response_fields)):
            duplicates = _duplicate_names(fields)
            if duplicates:
                problems.append(f"{name} repeats {label} field(s) {', '.join(duplicates)}")
        return problems


class Contract(BaseModel):
    """Shared interface the scaffold task writes and every feature task codes against."""

    routes: list[ContractRoute] = Field(default_factory=list)
    error_shape: list[FieldSpec] = Field(
        default_factory=list,
        description="Fields of the one error body every api route returns on failure",
    )
    modules: list[str] = Field(
        default_factory=list,
        description="Public signatures as short free-text lines",
    )
    shared_files: list[str] = Field(
        default_factory=list,
        description=(
            "Seams the scaffold owns and features code against: mount points, "
            "router-registration spots, db helpers, stable element ids. Never a "
            "deps manifest or an entry file the scaffold recipe already created."
        ),
    )


class Task(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(
        min_length=1,
        description=(
            "Workspace-relative paths this task alone may write, including the root "
            "prefix (e.g. 'frontend/src/pages/Auth.jsx', not 'src/pages/Auth.jsx'). "
            "A scaffold owns its seam files (entry point, wiring, manifest, env "
            "example), never its entire root."
        ),
    )
    assignee: str | None = None
    kind: Literal["scaffold", "feature"] = "feature"
    root: str = Field(
        default="",
        description=(
            "Which Spec workspace root this task builds in ('' = workspace root). "
            "Every owned path lives inside it."
        ),
    )
    required_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Built-in skill names this task needs attached (validated against the "
            "built-in catalog). Project skills can never satisfy a requirement."
        ),
    )
    covers: list[str] = Field(
        default_factory=list,
        description="Acceptance criterion ids this task is meant to satisfy",
    )

    @model_validator(mode="after")
    def normalize_paths(self) -> "Task":
        self.owned_paths = [safe_relative_path(path) for path in self.owned_paths]
        root = (self.root or "").strip().rstrip("/")
        self.root = "" if root in ("", ".") else safe_relative_path(root)
        if self.root:
            # Owned paths are workspace-relative. A planner that writes them
            # relative to the task's root ('src/pages/Auth.jsx' in root
            # 'frontend') produced leases on files that do not exist and hid
            # real overlaps from the ownership check (run-7f74e69c: feature
            # workers wrote stray app/ and src/ trees at the workspace root
            # while the scaffold's whole-root lease covered their files).
            prefix = self.root + "/"
            self.owned_paths = [
                path if path == self.root or path.startswith(prefix) else prefix + path
                for path in self.owned_paths
            ]
        return self


class TaskGraph(BaseModel):
    spec_title: str = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)
    contract: Contract | None = None
    deferred: list[str] = Field(
        default_factory=list,
        description=(
            "Acceptance criterion ids deliberately left for a later run because "
            "the whole spec cannot be built in this one. Every criterion is either "
            "covered by a task or listed here — never both, never neither. Leave "
            "empty when the whole spec is planned."
        ),
    )

    def in_scope(self, criterion_ids: list[str]) -> list[str]:
        """The spec's criteria this run builds (and the suite therefore judges)."""
        parked = set(self.deferred)
        return [cid for cid in criterion_ids if cid not in parked]

    @model_validator(mode="after")
    def deferred_scope_is_explicit(self, info: ValidationInfo) -> "TaskGraph":
        """Given the spec's criterion ids (pydantic context), no criterion may be
        silently dropped or both built and deferred. Without context (tests,
        one-off graphs) only the self-consistent checks apply. A graph that
        tracks no coverage at all (no covers, nothing deferred) is accepted as
        before — blame then falls back to the scaffold — but a graph that tracks
        coverage must track all of it."""
        if len(self.deferred) != len(set(self.deferred)):
            raise ValueError("deferred criterion ids must be unique")
        covered = {cid for task in self.tasks for cid in task.covers}
        both = covered & set(self.deferred)
        if both:
            raise ValueError(
                f"criteria both covered by a task and deferred: {', '.join(sorted(both))}"
            )
        if (info.context or {}).get("precise_contract") and self.contract is not None:
            # two developers build both sides of every api route at once: the
            # fields must be exact before either starts
            problems = [p for route in self.contract.routes for p in route.vagueness()]
            if problems:
                raise ValueError(
                    "the contract must list exact fields for every api route: "
                    + "; ".join(problems[:8])
                )
        criterion_ids = (info.context or {}).get("criterion_ids")
        if criterion_ids is not None and (covered or self.deferred):
            known = set(criterion_ids)
            unknown = (set(self.deferred) | covered) - known
            if unknown:
                raise ValueError(
                    f"covers/deferred name unknown criteria: {', '.join(sorted(unknown))}"
                )
            dropped = known - covered - set(self.deferred)
            if dropped:
                raise ValueError(
                    "every acceptance criterion must be covered by a task or listed "
                    f"in deferred; missing: {', '.join(sorted(dropped))}"
                )
            if known and set(self.deferred) == known:
                raise ValueError("deferred lists every criterion; this run would build nothing")
        return self

    @model_validator(mode="after")
    def ownership_and_dependencies(self) -> "TaskGraph":
        self._validate_required_skills()
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        owned: list[str] = []
        deps: dict[str, set[str]] = {}
        scaffolds = [task for task in self.tasks if task.kind == "scaffold"]
        self._validate_scaffolds(scaffolds)
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

    @staticmethod
    def _validate_required_skills_static(tasks: list["Task"]) -> None:
        from kiln.agents.skills import SKILL_CATALOG

        for task in tasks:
            for name in task.required_skills:
                if name not in SKILL_CATALOG:
                    raise ValueError(
                        f"{task.id}: unknown required skill {name!r} "
                        "(must be a built-in from the skill catalog)"
                    )

    def _validate_required_skills(self) -> None:
        # Built-in names only. Project skills are untrusted overlays and can
        # never satisfy a requirement, so we check the built-in catalog keys,
        # never the effective (merged) catalog.
        self._validate_required_skills_static(self.tasks)

    def _validate_scaffolds(self, scaffolds: list[Task]) -> None:
        """At most one scaffold per root; a scaffold owns seams, not its root.

        Multi-root products have one scaffold per disjoint root (backend/ +
        frontend/). Single-root apps keep the historical one-scaffold rule.
        """
        roots: list[str] = []
        for scaffold in scaffolds:
            key = scaffold.root or (scaffold.owned_paths[0] if scaffold.owned_paths else "")
            if key in roots:
                raise ValueError(f"multiple scaffold tasks for root {key!r}")
            roots.append(key)
        # A scaffold that owns its whole root while features own files inside
        # it is the run-7f74e69c breach: the scaffold's lease covered every
        # feature file, so it wrote them all. Reject with the fix spelled out
        # (this message reaches the planner's retry).
        for scaffold in scaffolds:
            root = scaffold.root
            if not root or root not in scaffold.owned_paths:
                continue
            inside = [
                path
                for task in self.tasks
                if task is not scaffold
                for path in task.owned_paths
                if path.startswith(root + "/")
            ]
            if inside:
                raise ValueError(
                    f"scaffold {scaffold.id} owns the whole root {root!r}, but other tasks "
                    f"own files inside it (e.g. {inside[0]}). A scaffold owns only its seam "
                    f"files inside the root — entry point, router wiring, manifest, env "
                    f"example (e.g. {root}/app/main.py) — never the entire root."
                )


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
