"""The full loop: idea to tested app, driven as one async orchestration.

Requirements may ask the user; the stack is probed and installed on the host
with approval; developers run as tool-using agents with path leases so two
devs cannot overwrite each other; tests run on the host by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from kiln.agents import fixer, planner, reporter, requirements, reviewer, test_author, worker
from kiln.agents.definitions import definition_for
from kiln.agents.planner import CONTRACT_DOC, DESIGN_DOC, PLAN_DOC, render_plan_markdown
from kiln.agents.roles import Role
from kiln.agents.skills import (
    SKILL_CATALOG,
    clear_project_skills,
    frameworks_for_root,
    needs_prod_ops_skill,
    needs_security_skill,
    needs_ui_product_skill,
    register_project_skill_texts,
    resolve_skills,
    root_is_api_only,
    wrap_call_tool_with_skills,
)
from kiln.agents.transcript import TranscriptLog
from kiln.companion.protocol import AgentScope
from kiln.companion.registry import NO_COMPANION as _NO_COMPANION
from kiln.companion.registry import CompanionRegistry, CompanionUnavailable, ToolDenied
from kiln.config import Settings, load_settings
from kiln.events.schema import (
    AgentRef,
    AgentStatus,
    MessageSent,
    RunFinished,
    RunStarted,
    TaskAssigned,
    TaskRef,
    TestRunFinished,
    UsageTick,
)
from kiln.gateway.client import Gateway
from kiln.agents.context import OFFLOADED_PREFIX
from kiln.graph.lanes import TeamBoard, parallel_estimate
from kiln.graph.lead import LeadMixin
from kiln.graph.shell_hints import shell_hint
from kiln.schemas.supervisor import Escalation
from kiln.paths import paths_conflict, safe_relative_path
from kiln.product.browser_flows import fallback_flow_plan
from kiln.schemas.browser_flow import BROWSER_FLOWS_DOC
from kiln.schemas.results import TestReport
from kiln.schemas.review import ReviewVerdict
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import FieldSpec, Task, TaskGraph
from kiln.schemas.work import WorkerResult, WorkFile
from kiln.stacks.profiles import profile_for

FloorSend = Callable[[object], Awaitable[None]]

logger = logging.getLogger("kiln.orchestrator")

# Top-level entries that do not make a folder an existing project: VCS,
# environments, editor state and Kiln's own run artifacts.
_INVENTORY_IGNORE = frozenset(
    {
        ".git/",
        ".kiln/",
        ".venv/",
        "venv/",
        "node_modules/",
        "__pycache__/",
        ".idea/",
        ".vscode/",
        ".env",
        ".gitignore",
        ".DS_Store",
    }
)

# Fix passes in a row that did not lower the failure count before the loop
# gives up: a second identical failure count means the fixer is not converging.
FIX_STALL_LIMIT = 2

# The companion appends this to a shell result when the command changed files
# outside the caller's lease; the developer's teammate is told about it.
_LEAK_MARKER = "[warning: this command changed files outside your lease:"

DEMO_AGENTS = {
    Role.REQUIREMENTS: AgentRef(
        id="requirements", role=Role.REQUIREMENTS, label="Riley Requirements"
    ),
    Role.PLANNER: AgentRef(id="planner", role=Role.PLANNER, label="Pat Planner"),
    Role.TEST_AUTHOR: AgentRef(id="test_author", role=Role.TEST_AUTHOR, label="Tessa Tests"),
    Role.FIXER: AgentRef(id="fixer", role=Role.FIXER, label="Felix Fixer"),
    Role.REVIEWER: AgentRef(id="reviewer", role=Role.REVIEWER, label="Remy Reviewer"),
    Role.REPORTER: AgentRef(id="reporter", role=Role.REPORTER, label="Rosa Reporter"),
    Role.SUPERVISOR: AgentRef(id="supervisor", role=Role.SUPERVISOR, label="Lena Lead"),
}


class LoopError(RuntimeError):
    """The loop cannot continue; the run ends honestly, not green."""


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _first_lines(text: str, lines: int = 4, limit: int = 400) -> str:
    """The head of a tool error, ANSI colours stripped, for a one-line reason."""
    clean = _ANSI.sub("", text).strip()
    head = " | ".join(line.strip() for line in clean.splitlines()[:lines] if line.strip())
    return head[:limit]


@dataclass
class _FileRequest:
    """A developer's queued request for files its teammate holds."""

    lane: str
    paths: list[str]
    reason: str
    extend: Callable[[], Awaitable[None]]
    notify: bool  # tell the developer by message (it kept working) vs wake its wait
    event: asyncio.Event = field(default_factory=asyncio.Event)
    granted: bool = False


class _SuiteState:
    """Regression context carried between build steps for <test_state>."""

    def __init__(self) -> None:
        self.known_green: list[str] = []
        self.known_broken = 0
        self.regressions: list[str] = []

    def block(self) -> str:
        return worker.test_state_block(self.known_green, self.known_broken, self.regressions)


def dependency_layers(graph: TaskGraph) -> list[list[Task]]:
    """Group tasks so each layer only depends on earlier layers."""
    remaining = {task.id: task for task in graph.tasks}
    done: set[str] = set()
    layers: list[list[Task]] = []
    while remaining:
        ready = [task for task in remaining.values() if set(task.depends_on) <= done]
        if not ready:
            raise LoopError("task dependencies cannot be scheduled")
        layers.append(ready)
        done.update(task.id for task in ready)
        for task in ready:
            del remaining[task.id]
    return layers


def _fallback_owners(graph: TaskGraph) -> list[Task]:
    """Nothing maps to the failure: the shared scaffold is the honest suspect
    (imports, wiring, config), not whichever task happens to be first. With no
    scaffold task the failure is run-wide — widen to every task rather than
    blame an arbitrary owner."""
    scaffolds = [task for task in graph.tasks if task.kind == "scaffold"]
    return scaffolds or list(graph.tasks)


def _case_in_file(nodeid: str, path: str) -> bool:
    """Does a JUnit test id belong to this suite file?

    Two id styles reach us: path-style (vitest: ``tests/api.test.ts::…``) and
    pytest's dotted module classname (``tests.test_api::test_x``, or
    ``tests.test_api.TestNotes::test_x`` for a test class). Matching only the
    path style meant no pytest failure ever mapped to a task: blame fell
    through to the scaffolds and the fixer was handed the wrong files on every
    Python fix round (caught by the e2e simulator's fix_loop scenario).
    """
    nid = nodeid.replace("\\", "/")
    if path in nid or path.rsplit("/", 1)[-1] in nid:
        return True
    classname = nid.split("::", 1)[0]
    dotted = path.rsplit(".", 1)[0].replace("/", ".")
    return classname == dotted or classname.startswith(dotted + ".")


def blame_tasks(report: TestReport, graph: TaskGraph, suite: TestSuite | None) -> list[Task]:
    """Map failing tests to tasks via suite covers → criterion → task.covers."""
    failing = [case for case in report.cases if case.outcome in ("failed", "error")]
    if not failing:
        return _fallback_owners(graph)

    criterion_hits: set[str] = set()
    if suite is not None:
        for case in failing:
            for file in suite.files:
                if _case_in_file(case.nodeid, file.path):
                    criterion_hits.update(file.covers)

    tasks: list[Task] = []
    if criterion_hits:
        for task in graph.tasks:
            if set(task.covers) & criterion_hits:
                tasks.append(task)
    if not tasks:
        blob = " ".join(case.nodeid for case in failing).lower()
        for task in graph.tasks:
            stem = task.id.lower().replace("-", "_")
            if stem in blob or any(p.split("/")[0].lower() in blob for p in task.owned_paths):
                tasks.append(task)
    return tasks or _fallback_owners(graph)


class Orchestrator(LeadMixin):
    def __init__(
        self,
        gateway: Gateway,
        registry: CompanionRegistry,
        send: FloorSend,
        ceiling: int,
        settings: Settings | None = None,
        transcript: TranscriptLog | None = None,
    ):
        self.gateway = gateway
        self.registry = registry
        self.send = send
        self.ceiling = ceiling
        self.settings = settings or load_settings()
        self.transcript = transcript or TranscriptLog()
        self.worker_results: list[WorkerResult] = []
        self.stack_id = "python"
        self._suite: TestSuite | None = None
        self._spec = None
        self._ui_product = False
        self._prod_ops = False
        self._security = False
        # frameworks from the Spec drive deterministic scaffolding + skill packs
        self._frameworks: tuple[str, ...] = ()
        self._scaffolded: dict[str, str] = {}  # root -> recipe id
        self._worker_seq = 0
        # Top-level workspace entries at run start; non-empty = an existing
        # project the brief changes, which the analyst and planner are told.
        self._inventory: list[str] = []
        # Files Kiln wrote this run: anything else that exists pre-dates the run
        # and is the user's (never clobbered in an existing project).
        self._kiln_written: set[str] = set()
        self._deferred: list[str] = []
        self._plan_memo = ""
        self._briefing_cache: dict[str, str] | None = None
        # what each finished task's worker reported, and which worker built it
        self._results_by_task: dict[str, WorkerResult] = {}
        self._task_agent: dict[str, str] = {}
        self._stage: tuple[str, Role | None] = ("starting", None)
        # the two developers and how the build is going
        self._board: TeamBoard | None = None
        self._failed_tasks: dict[str, str] = {}  # task id -> why it was not delivered
        self._lanes_tried: dict[str, set[str]] = {}  # task id -> lanes that failed it
        self._retry_notes: dict[str, str] = {}  # task id -> note for its next attempt
        # what went wrong and how the run recovered — the reporter states each one
        self._run_notes: list[str] = []
        self._suite_healed = False
        self._file_requests: list[_FileRequest] = []
        # the approved plan, the contract's generated artifacts, and the lock
        # that serializes contract changes
        self._graph: TaskGraph | None = None
        self._contract_files: list[str] = []
        self._contract_lock = asyncio.Lock()
        # the tech lead and the background work it (and the floor) spawns
        self._run_id = ""
        self._idea = ""
        self._building = False
        self._added_task_ids: set[str] = set()
        self._decisions: list[str] = []
        self._supervisor_calls = 0
        self._supervisor_lock = asyncio.Lock()
        self._supervisor_jobs: set[asyncio.Task] = set()
        self._background: set[asyncio.Task] = set()
        self._shell_failures: dict[tuple[str, str, str], int] = {}
        # root path → "installed" / "failed" once install_deps ran there
        self._root_state: dict[str, str] = {}
        self._root_locks: dict[str, asyncio.Lock] = {}
        self._companion_away = False
        self._companion_away_since: float | None = None
        # Product gates that could not run on this host (no browser, no start
        # command). For a UI product these block a green verdict — a UI that was
        # never exercised in a browser is not verified, whatever the suite says.
        self._blocked_gates: list[str] = []

    def _env(self, run_id: str) -> dict:
        run = self.registry.get_run(run_id)
        return dict(run.environment) if run else {}

    async def _product_briefing(self, run_id: str) -> dict[str, str]:
        """Design + discovery for coding agents when inject_product_briefing is on.

        Cached: every worker used to re-read both documents. The cache is
        dropped whenever a task writes either one (the UI scaffold owns
        docs/design.md), so later workers see the version that landed.
        """
        if not self.settings.inject_product_briefing:
            return {"design": "", "discovery": ""}
        if self._briefing_cache is None:
            docs = await self.read_workspace(run_id, [DESIGN_DOC, requirements.DISCOVERY_DOC])
            self._briefing_cache = {
                "design": docs.get(DESIGN_DOC) or "",
                "discovery": docs.get(requirements.DISCOVERY_DOC) or "",
            }
        return dict(self._briefing_cache)

    def _note_written(self, paths) -> None:
        """Record Kiln's own writes; drop the briefing cache if a brief doc changed."""
        for path in paths:
            self._kiln_written.add(path)
            if path in (DESIGN_DOC, requirements.DISCOVERY_DOC):
                self._briefing_cache = None

    async def _take_inventory(self, run_id: str) -> list[str]:
        """Top-level workspace entries that make this an existing project."""
        try:
            listing = await self.call_tool(run_id, "list_dir", {"path": "."})
        except Exception:  # noqa: BLE001 — an unreadable root is treated as fresh
            return []
        entries = [line.strip() for line in (listing or "").splitlines() if line.strip()]
        if entries == ["(empty)"]:
            return []
        return [entry for entry in entries if entry not in _INVENTORY_IGNORE]

    async def _preexisting(self, run_id: str, path: str) -> bool:
        """True when ``path`` is a user file in an existing project.

        Only an existing project has user files to protect, and only files Kiln
        did not write this run count — a README Kiln wrote in run 1 of a fresh
        folder may be rewritten by run 1's reporter.
        """
        if not self._inventory or path in self._kiln_written:
            return False
        try:
            await self.call_tool(run_id, "read_file", {"path": path, "limit": 1})
        except Exception:  # noqa: BLE001 — missing file: nothing to protect
            return False
        return True

    async def _protect_suite(self, run_id: str, suite: TestSuite) -> TestSuite:
        """Rename suite files that would overwrite the user's existing tests.

        The user's tests stay (and keep running as regression checks); the new
        file gets a ``_kiln`` suffix. The suite object is rewritten too, so blame
        still maps the renamed files to their criteria.
        """
        renamed = []
        changed = False
        for file in suite.files:
            path = file.path
            if await self._preexisting(run_id, path):
                stem, dot, ext = path.rpartition(".")
                candidate = f"{stem}_kiln{dot}{ext}"
                n = 2
                while await self._preexisting(run_id, candidate):
                    candidate = f"{stem}_kiln{n}{dot}{ext}"
                    n += 1
                logger.info("existing test %s kept; new suite file written as %s", path, candidate)
                path = candidate
                changed = True
            renamed.append(file.model_copy(update={"path": path}))
        if not changed:
            return suite
        return suite.model_copy(update={"files": renamed})

    async def _protected_target(self, run_id: str, path: str) -> str:
        """Where a reporter document goes without clobbering the user's file."""
        if path in ("README.md", "AGENTS.md") and await self._preexisting(run_id, path):
            return f"docs/kiln-{path.lower()}"
        return path

    async def _load_project_skills(self, run_id: str) -> None:
        """Snapshot workspace `.kiln/skills` (+ optional `.kiln/agents`) into the skill catalog."""
        if not self.settings.enable_project_skills:
            clear_project_skills()
            return
        scope = self._scope("orchestrator", "Kiln", Role.PLANNER)
        paths: list[str] = []
        for pattern, prefix in (
            (".kiln/skills/*", ".kiln/skills/"),
            (".kiln/agents/*", ".kiln/agents/"),
        ):
            try:
                listing = await self.call_tool(
                    run_id, "glob", {"pattern": pattern}, agent=scope
                )
            except Exception:
                continue
            if not listing or listing.strip() == "(no matches)":
                continue
            paths.extend(
                line.strip()
                for line in listing.splitlines()
                if line.strip()
                and line.strip().endswith((".md", ".txt"))
                and line.strip().startswith(prefix)
            )
        if not paths:
            clear_project_skills()
            return
        files = await self.read_workspace(run_id, paths[:20])
        register_project_skill_texts(files, enabled=True)

    def _scope(
        self, agent_id: str, label: str, role: Role, owned: list[str] | None = None
    ) -> dict:
        return AgentScope(
            id=agent_id, label=label, role=role.value, owned_paths=list(owned or [])
        ).model_dump()

    async def status(self, role: Role, status: str, detail: str) -> None:
        await self.send(AgentStatus(agent=DEMO_AGENTS[role], status=status, detail=detail))

    async def usage_tick(self) -> None:
        summary = self.gateway.usage.summary()
        await self.send(
            UsageTick(
                tokens_used=summary["prompt_tokens"] + summary["completion_tokens"],
                ceiling=self.ceiling,
                calls=summary["calls"],
            )
        )

    async def call_tool(
        self,
        run_id: str,
        tool: str,
        tool_input: dict,
        agent: dict | None = None,
        *,
        record: bool = True,
    ) -> str:
        aid = (agent or {}).get("id", "orchestrator")
        role = (agent or {}).get("role", "orchestrator")
        # Agents driven by run_agent_loop already log every call there; a
        # second record here doubled each one in the transcript (run-9f836de2
        # showed pairs of identical calls). Only orchestrator-driven calls
        # (claim_scope, read_workspace, plan/requirements tool loops) record here.
        if record:
            self.transcript.record(aid, role, "tool_call", {"name": tool, "args": tool_input})
        try:
            output = await self._call_through_outage(run_id, tool, tool_input, agent)
        except Exception as exc:
            if record:
                self.transcript.record(aid, role, "tool_result", {"name": tool, "error": str(exc)})
            raise
        if record:
            self.transcript.record(aid, role, "tool_result", {"name": tool, "output": output})
        return output

    async def _call_through_outage(
        self, run_id: str, tool: str, tool_input: dict, agent: dict | None
    ) -> str:
        """Relay one call; while no companion is attached, hold it instead of failing.

        The registry waits up to 90s for a companion and then refuses. A call
        refused that way never reached a companion, so repeating it is safe —
        and holding it pauses the agent (no model calls) until the companion is
        back. run-aa748f20's developers spent model calls, a compaction and an
        escalation on eight minutes of "no companion attached" errors. A call
        cut off mid-flight is never repeated (a partial write or shell is not
        idempotent): the registry raises that one differently.
        """
        while True:
            try:
                output = await self.registry.call_tool(
                    run_id, tool, tool_input, timeout=300, agent=agent
                )
            except CompanionUnavailable as exc:
                if type(exc) is not CompanionUnavailable or str(exc) != _NO_COMPANION:
                    raise
                # one deadline per outage, shared by every waiting call: a
                # companion that never returns fails calls fast after it
                now = time.monotonic()
                if self._companion_away_since is None:
                    self._companion_away_since = now
                if now - self._companion_away_since >= self.settings.companion_outage_wait_seconds:
                    raise
                if not self._companion_away:
                    self._companion_away = True
                    self._run_notes.append("the companion disconnected mid-build; the team paused for it")
                    await self.send(MessageSent(
                        from_agent="kiln", to_agent="client",
                        summary="the companion disconnected — the team is paused until it reconnects "
                        "(restart `kiln-companion` if it closed)",
                    ))
                continue
            self._companion_away_since = None
            if self._companion_away:
                self._companion_away = False
                await self.send(MessageSent(
                    from_agent="kiln", to_agent="client",
                    summary="the companion is back — the team continues",
                ))
            return output

    async def write_files(self, run_id: str, files, agent: dict | None = None) -> None:
        """Orchestrator-driven writes (contract, reporter docs, non-agentic patch).

        The companion's ReadLedger refuses an overwrite the writing identity
        never read. When the orchestrator writes under an agent scope (the
        reporter's README, the planner's contract, a non-agentic fixer's
        patch) that agent may never have read the target — run-9f836de2 died
        on exactly this after a green suite. Pre-read each existing target
        under the same scope so the ledger is satisfied; a missing file needs
        no read (the executor only checks the ledger for existing files).
        """
        for file in files:
            try:
                await self.call_tool(run_id, "read_file", {"path": file.path}, agent=agent)
            except Exception:
                pass  # new file: no prior read required
            await self.call_tool(
                run_id,
                "write_file",
                {"path": file.path, "content": file.content},
                agent=agent,
            )
            self._note_written([file.path])

    async def read_workspace(self, run_id: str, paths: list[str]) -> dict[str, str]:
        contents: dict[str, str] = {}
        for path in paths:
            try:
                contents[path] = await self.call_tool(run_id, "read_file", {"path": path})
            except Exception:
                continue
        return contents

    async def run_tests(self, run_id: str) -> TestReport:
        output = await self.call_tool(
            run_id, "run_tests", {"stack_id": self.stack_id}
        )
        if output.startswith(OFFLOADED_PREFIX):
            raise LoopError(
                "the companion offloaded the test report instead of returning it — "
                "restart the companion so it runs the current Kiln version"
            )
        return TestReport.model_validate_json(output)

    def _at(self, stage: str, role: Role | None = None) -> None:
        """Mark what the run is doing, so a crash is reported where it happened."""
        self._stage = (stage, role)

    async def run(self, run_id: str, idea: str, user_notes: list[str]) -> bool:
        started = time.monotonic()
        await self.send(RunStarted(run_id=run_id, idea=idea))
        self._at("starting")
        try:
            green = await self._phases(run_id, idea, tuple(user_notes))
        except Exception as exc:
            logger.exception("run %s failed", run_id)
            # Report on the stage that failed. Every crash used to land on the
            # reporter ("Rosa Reporter stuck"), which read as a docs failure
            # when run-9bbe61f0 actually died at its first test run.
            stage, role = self._stage
            detail = f"run failed during {stage}: {_first_lines(str(exc), limit=300)}"
            if role in DEMO_AGENTS:
                await self.send(
                    AgentStatus(agent=DEMO_AGENTS[role], status="stuck", detail=detail[:200])
                )
            await self.send(MessageSent(from_agent="kiln", to_agent="client", summary=detail))
            green = False
        # floor messages and late lead decisions finish before the run closes
        for job in list(self._supervisor_jobs):
            job.cancel()
        await self._drain_background(timeout=5)
        wall = time.monotonic() - started
        await self.send(
            RunFinished(
                run_id=run_id,
                green=green,
                wall_clock_seconds=round(wall, 1),
                notes=list(self._run_notes)[-20:],
            )
        )
        return green

    def _notes(self, run_id: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        run = self.registry.get_run(run_id)
        if run is None or not run.user_notes:
            return fallback
        return tuple(run.user_notes)

    async def _resolve_stack(self, run_id: str, stack_id: str) -> None:
        await self.status(Role.REQUIREMENTS, "working", f"checking {stack_id} on your machine")
        scope = self._scope("requirements", "Riley Requirements", Role.REQUIREMENTS)
        raw = await self.call_tool(
            run_id, "probe_stack", {"stack_id": stack_id}, agent=scope
        )
        report = json.loads(raw)
        if report.get("ready"):
            self.stack_id = stack_id
            return
        for key, info in (report.get("tools") or {}).items():
            if info.get("ok") or info.get("installed_with"):
                continue
            if not info.get("install"):
                raise LoopError(
                    f"{info.get('label', key)} is missing and no installer is available; "
                    "install it yourself or pick another stack"
                )
            await self.call_tool(
                run_id, "install_tool", {"tool": key}, agent=scope
            )
        raw = await self.call_tool(
            run_id, "probe_stack", {"stack_id": stack_id}, agent=scope
        )
        report = json.loads(raw)
        if not report.get("ready"):
            raise LoopError(
                f"stack {stack_id} is still not ready after install; "
                "on Windows you may need to re-run the companion so PATH refreshes"
            )
        self.stack_id = stack_id

    def _skill_kwargs(
        self, role: Role | str = "", required: tuple[str, ...] = (), root: str | None = None
    ) -> dict:
        """Skill packs for one agent; ``root`` scopes them to a task's root.

        A task in a declared root gets only that root's framework pack, and a
        task in an API-only root drops the UI product pack — a backend worker
        does not need Vite/React or landing-page rules in every turn.
        """
        role_value = role.value if isinstance(role, Role) else str(role)
        frameworks = self._frameworks
        ui_product = self._ui_product
        if root is not None:
            frameworks = frameworks_for_root(self._spec, root) or self._frameworks
            if root_is_api_only(self._spec, root):
                ui_product = False
        return {
            "ui_product": ui_product,
            "prod_ops": self._prod_ops,
            "security": self._security,
            "extra_skills": resolve_skills(
                role=role_value,
                frameworks=frameworks,
                required=required,
            ),
        }

    def _root_of(self, tasks: list[Task]) -> str | None:
        """The one root a set of tasks shares, or None (mixed roots → full set)."""
        roots = {task.root for task in tasks}
        return roots.pop() if len(roots) == 1 else None

    async def _scaffold_roots(self, run_id: str, spec, graph: TaskGraph) -> None:
        """Deterministically scaffold every Spec root before any worker starts.

        The companion runs the official recipe (pinned argv or template); the
        scaffold *task* then only adapts seams. Recorded once in the transcript
        under the orchestrator so the agent loop does not double-log it. Roots
        with no declared framework are skipped.

        A scaffold that fails or is declined must not leave every later agent
        building on a skeleton that is not there (run-9bbe61f0 wrote a Next.js
        app with no package.json, then died at its first test run). It falls
        back to a hand-built skeleton: the root's scaffold task is told the
        recipe could not run and is leased the essential files to create.
        """
        for root in spec.roots:
            framework = root.framework
            if not framework:
                continue
            try:
                await self.call_tool(
                    run_id,
                    "scaffold_framework",
                    {"framework": framework, "root": root.path, "options": {}},
                    agent=self._scope("orchestrator", "Kiln", Role.PLANNER),
                )
                self._scaffolded[root.path] = framework
            except ToolDenied as exc:
                await self._skeleton_fallback(graph, root.path, framework, f"declined: {exc}")
            except Exception as exc:  # noqa: BLE001 — classified below
                if "already scaffolded" in str(exc):
                    # an existing project: the root is reused, not a failure
                    self._scaffolded[root.path] = framework
                    logger.info("%s at %s already scaffolded; reusing it", framework, root.path)
                    continue
                logger.warning("scaffold_framework(%s @ %s) failed: %s", framework, root.path, exc)
                await self._skeleton_fallback(graph, root.path, framework, _first_lines(str(exc)))

    async def _write_contract_artifacts(self, run_id: str, spec, graph: TaskGraph) -> list[str]:
        """Write the machine-readable contract and each root's generated types.

        The types modules are owned by no task — neither developer can edit
        them by hand; they change only when a contract change is accepted. A
        path a task owns, or a user file in an existing project, is skipped.
        Never fatal: a failed write is logged and the build goes on.
        """
        from kiln.contract import CONTRACT_JSON, contract_json, render_types, types_targets

        if graph.contract is None:
            return []
        files = [WorkFile(path=CONTRACT_JSON, content=contract_json(graph.contract))]
        owned = [p for task in graph.tasks for p in task.owned_paths]
        for path, language in types_targets(spec, graph.contract):
            if any(paths_conflict(path, other) for other in owned):
                self._note_once(f"shared types not generated at {path}: a task owns that path")
                continue
            if await self._preexisting(run_id, path):
                self._note_once(f"shared types not generated at {path}: a file of yours is there")
                continue
            files.append(WorkFile(path=path, content=render_types(graph.contract, language)))
        written: list[str] = []
        for file in files:
            try:
                await self.write_files(run_id, [file])
                written.append(file.path)
            except Exception as exc:  # noqa: BLE001 — artifacts help, they never gate
                logger.warning("could not write %s: %s", file.path, exc)
        self._contract_files = [p for p in written if p != CONTRACT_JSON]
        return written

    def _contract_brief(self) -> str:
        """Where the generated types live, for the developers' briefs."""
        from kiln.contract import CONTRACT_JSON

        if self._graph is None or self._graph.contract is None:
            return ""
        lines = [
            "<contract_types>",
            f"The contract above is the source of truth (machine-readable: {CONTRACT_JSON}).",
        ]
        if self._contract_files:
            lines.append(
                "Generated types for each side — import them instead of re-declaring "
                "request/response shapes: " + ", ".join(self._contract_files) + "."
            )
        lines.append(
            "Never edit the generated files or code around the contract: if a route or "
            "field is wrong or missing, call propose_contract_change. If a detail is "
            "only known to your teammate, ask_peer."
        )
        lines.append("</contract_types>")
        return "\n".join(lines)

    async def _skeleton_fallback(
        self, graph: TaskGraph, root_path: str, framework: str, reason: str
    ) -> None:
        """Hand the skeleton to the root's scaffold task (or its first task).

        The task is leased the recipe's essential files that no other task
        owns, and its brief says to create a minimal, standard skeleton. If the
        root has no task at all, the failure is reported and the run goes on.
        """
        from kiln.stacks.scaffold_recipes import skeleton_files

        key = "" if root_path in ("", ".") else root_path.strip().rstrip("/")
        prefix = f"{key}/" if key else ""
        in_root = [task for task in graph.tasks if (task.root or "") == key]
        target = next((t for t in in_root if t.kind == "scaffold"), None) or (in_root[0] if in_root else None)
        summary = f"the official {framework} scaffold at {root_path!r} could not run ({reason})"
        if target is None:
            self._run_notes.append(f"{summary}; no task builds in that root, so no skeleton was made")
            await self.send(MessageSent(from_agent="kiln", to_agent="client", summary=summary[:300]))
            return
        wanted = [f"{prefix}{name}" for name in skeleton_files(framework)]
        others = [p for t in graph.tasks if t.id != target.id for p in t.owned_paths]
        extra = [
            path
            for path in wanted
            if path not in target.owned_paths
            and not any(paths_conflict(path, other) for other in others)
        ]
        required = list(target.required_skills)
        if framework in SKILL_CATALOG and framework not in required:
            required.append(framework)
        patched = target.model_copy(
            update={
                "owned_paths": [*target.owned_paths, *extra],
                "required_skills": required,
                "description": target.description
                + f"\n\nSKELETON FALLBACK: {summary}. Create the minimal, standard "
                f"{framework} skeleton yourself before your seams: "
                f"{', '.join(wanted) or 'the manifest and config'} (inside your owned paths). "
                "Use the framework's usual layout and pinned major versions; do not invent "
                "a different bundler or framework.",
            }
        )
        graph.tasks[graph.tasks.index(target)] = patched
        if self._board is not None:
            self._board.update_task(patched)
        self._run_notes.append(f"{summary}; {target.id} built the skeleton by hand instead")
        await self.send(
            MessageSent(
                from_agent="kiln",
                to_agent="client",
                summary=f"{summary[:200]} — {target.id} will build the skeleton by hand",
            )
        )

    async def _run_planner(self, spec, notes: tuple[str, ...], plan_tool) -> TaskGraph:
        tool = wrap_call_tool_with_skills(plan_tool)
        if self.settings.agentic_devs:
            return await planner.run_agentic(
                self.gateway,
                spec,
                notes,
                call_tool=tool,
                inventory=self._inventory or None,
                **self._skill_kwargs(Role.PLANNER),
            )
        return await asyncio.to_thread(
            planner.run,
            self.gateway,
            spec,
            notes,
            inventory=self._inventory or None,
            **self._skill_kwargs(Role.PLANNER),
        )

    def _plan_lines(self, spec, graph: TaskGraph, lead: str) -> list[str]:
        """What the user approves: the tasks, who builds them, what is deferred."""
        lines = [lead]
        lanes = self._lane_count()
        assignment = {}
        if lanes > 1:
            from kiln.graph.lanes import assign_lanes

            assignment, _ = assign_lanes(graph, spec, lanes)
            steps, total = parallel_estimate(graph, spec, lanes)
            lines.append(f"two developers in parallel: about {steps} task-steps instead of {total}")
        for task in graph.tasks:
            dev = f" · Dev {assignment[task.id]}" if task.id in assignment else ""
            lines.append(f"{task.id} [{task.kind}]{dev} owns {', '.join(task.owned_paths)}")
        if graph.deferred:
            then = {c.id: c.then for c in spec.acceptance_criteria}
            lines.append(f"deferred to a later run ({len(graph.deferred)}):")
            lines.extend(f"  {cid}: {then.get(cid, '')}"[:160] for cid in graph.deferred)
        else:
            lines.append("nothing deferred — the whole spec is planned for this run")
        return lines

    async def _adopt_plan(self, run_id: str, graph: TaskGraph) -> TaskGraph:
        """Record the approved plan's scope and the planner's memo for the workers."""
        self._deferred = list(graph.deferred)
        self._plan_memo = (await self.read_workspace(run_id, [PLAN_DOC])).get(PLAN_DOC, "") or ""
        return graph

    @staticmethod
    def _smoke_paths(graph: TaskGraph | None) -> list[str]:
        """Paths the runtime smoke must GET: `/` plus the contract's page routes.

        Page routes are GET routes without path params and not under `/api`
        (those need auth/payloads). run-9f836de2 smoked only `/`, which returned
        the Landing page (200) while `/app` 404'd — the management UI was never
        checked. Deriving the paths from the contract catches a missing app
        route at runtime without a browser.
        """
        paths = ["/"]
        if graph is not None and graph.contract is not None:
            for route in graph.contract.routes:
                path = (route.path or "").strip()
                if not path or not path.startswith("/"):
                    continue
                if route.method.upper() != "GET":
                    continue
                if "{" in path or "}" in path or ":" in path:
                    continue
                if path.startswith("/api") and route.kind != "page":
                    continue
                if path != "/" and path not in paths:
                    paths.append(path)
        return paths

    async def _product_gates(
        self, run_id: str, idea: str, graph: TaskGraph | None = None
    ) -> list[str]:
        """Mechanical served-UI audit, host HTTP smoke, then Playwright flows.

        Returns only actionable (code-fixable) gaps. Host-env issues — missing
        Playwright, unspawnable start binary — are logged and dropped so they
        cannot force a fixer round that aborts a green suite.
        """
        from kiln.product.host_env import partition_product_gaps

        discovery = (await self.read_workspace(run_id, [requirements.DISCOVERY_DOC])).get(
            requirements.DISCOVERY_DOC, ""
        )
        gaps: list[str] = []
        try:
            raw = await self.call_tool(
                run_id,
                "served_ui_audit",
                {"idea": idea, "discovery": discovery},
            )
            payload = json.loads(raw)
            gaps.extend(payload.get("gaps") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("served_ui_audit failed: %s", exc)
        if self.settings.sandbox != "docker" and self._ui_product:
            try:
                raw = await self.call_tool(
                    run_id, "runtime_smoke", {"paths": self._smoke_paths(graph)}
                )
                payload = json.loads(raw)
                gaps.extend(payload.get("gaps") or [])
            except ToolDenied:
                pass
            except Exception as exc:  # noqa: BLE001
                # Spawn/PATH failures used to surface as FileNotFoundError and
                # still reach the fixer via browser_verify; keep them advisory.
                logger.warning("runtime_smoke failed (host env, not forcing fix): %s", exc)
            # Provision the browser BEFORE the verify gate: without this the gate
            # just reports BLOCKED_HOST_SETUP forever on a fresh host (PRP 5.1).
            # A declined/failed install leaves the gate blocked, not silently green.
            try:
                raw = await self.call_tool(run_id, "ensure_browser_runtime", {})
                provision = json.loads(raw)
                if provision.get("ready"):
                    # Record the pinned versions as a durable run fact (PRP 5.3) —
                    # this lands in events.jsonl, the run's queryable metadata.
                    installed = provision.get("installed") or []
                    pw_ver = provision.get("playwright_version") or "?"
                    ch_ver = provision.get("chromium_version") or "?"
                    version_note = (
                        f"browser runtime: playwright {pw_ver}, chromium {ch_ver}"
                        + (" (installed: " + ", ".join(installed) + ")" if installed else "")
                    )
                    logger.info("%s", version_note)
                    await self.send(
                        MessageSent(from_agent="kiln", to_agent="client", summary=version_note)
                    )
                else:
                    logger.warning(
                        "browser runtime not ready; the verify gate will be BLOCKED: %s",
                        provision.get("note"),
                    )
            except ToolDenied:
                # The user declined the install — the gate reports BLOCKED below.
                logger.warning("browser provisioning declined; the verify gate will be BLOCKED")
            except Exception as exc:  # noqa: BLE001
                logger.warning("ensure_browser_runtime failed (not forcing fix): %s", exc)
            try:
                raw = await self.call_tool(
                    run_id,
                    "browser_verify",
                    {"idea": idea, "discovery": discovery},
                )
                payload = json.loads(raw)
                gaps.extend(payload.get("gaps") or [])
            except ToolDenied:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser_verify failed (host env, not forcing fix): %s", exc)
        actionable, host_env, blocked = partition_product_gaps(gaps)
        for gap in host_env:
            logger.warning("product gate skipped (host env): %s", gap)
        for gap in blocked:
            logger.warning("product gate BLOCKED (host setup): %s", gap)
        self._blocked_gates.extend(blocked)
        return [f"product-gate: {g}" for g in actionable]

    async def _plan(self, run_id: str, spec, notes: tuple[str, ...]) -> TaskGraph:
        """Explore → plan → persist the contract → the user's approval beat.

        The planner gets a leased scope over just the plan documents, so its
        one allowed write is enforced by the companion, not by its prompt.
        """
        scope = self._scope("planner", "Pat Planner", Role.PLANNER, [PLAN_DOC, CONTRACT_DOC])

        async def plan_tool(name: str, args: dict) -> str:
            return await self.call_tool(run_id, name, args, agent=scope)

        await self.status(Role.PLANNER, "thinking", "exploring and splitting the spec into tasks")
        await self.call_tool(
            run_id,
            "claim_scope",
            {"owned_paths": [PLAN_DOC, CONTRACT_DOC], "task": "plan", "status": "working"},
            agent=scope,
        )
        try:
            graph = await self._run_planner(spec, self._notes(run_id, notes), plan_tool)
            graph = await self._parallelize(run_id, spec, notes, graph, plan_tool)
            # the durable contract: written by the orchestrator from the
            # validated graph, so it always matches what the workers receive
            await self.write_files(
                run_id,
                [WorkFile(path=CONTRACT_DOC, content=render_plan_markdown(spec, graph))],
                agent=scope,
            )
        finally:
            await self.call_tool(run_id, "release_scope", {}, agent=scope)
        await self.status(Role.PLANNER, "done", f"{len(graph.tasks)} tasks planned")
        await self.usage_tick()
        return await self._confirm_plan(run_id, spec, graph, scope)

    def _lane_count(self) -> int:
        return 1 if self.settings.max_parallel_devs <= 1 else 2

    async def _parallelize(
        self, run_id: str, spec, notes: tuple[str, ...], graph: TaskGraph, plan_tool
    ) -> TaskGraph:
        """One revision when a plan leaves the second developer nothing to do.

        A layer-by-layer plan (scaffold → models → routes → UI) is a chain:
        two developers take as long as one. With three or more tasks and no
        step where both could work, the planner is asked once for vertical
        slices with stubbed interfaces; the faster valid plan wins, and a
        failed revision keeps the original.
        """
        if self._lane_count() < 2 or len(graph.tasks) < 3:
            return graph
        steps, total = parallel_estimate(graph, spec, 2)
        if steps < total:
            return graph
        feedback = (
            f"plan feedback (parallelism): this graph is fully sequential — {total} tasks "
            f"take {steps} steps even with two developers, so one always waits. Re-split "
            "it for two developers in parallel: vertical feature slices that each own their "
            "model/route/component files; the scaffold stubs the shared interfaces (db "
            "helper, auth check, layout, module-discovering registration); keep depends_on "
            "for hard dependencies only. Same scope, same acceptance criteria."
        )
        try:
            revised = await self._run_planner(spec, (*self._notes(run_id, notes), feedback), plan_tool)
        except Exception as exc:  # noqa: BLE001 — the original plan still builds
            logger.warning("parallel replan failed; keeping the sequential plan: %s", exc)
            return graph
        revised_steps, _ = parallel_estimate(revised, spec, 2)
        if revised_steps < steps:
            self._note_once(
                f"the first plan was sequential ({steps} steps for {total} tasks); a "
                f"parallel re-split takes {revised_steps} steps for {len(revised.tasks)} tasks"
            )
            return revised
        return graph

    async def _confirm_plan(self, run_id: str, spec, graph: TaskGraph, scope: dict) -> TaskGraph:
        """Park the pipeline until the user approves the plan in the companion
        terminal. A refusal with feedback sends the planner back once; a second
        refusal ends the run honestly instead of building something unwanted."""
        from kiln.companion.registry import ToolDenied

        lines = self._plan_lines(spec, graph, f"{len(graph.tasks)} tasks:")
        try:
            await self.call_tool(
                run_id, "confirm_plan", {"lines": lines, "round": 1}, agent=scope
            )
            await self.status(Role.PLANNER, "done", "plan approved")
            return await self._adopt_plan(run_id, graph)
        except ToolDenied as exc:
            feedback = str(exc)
            await self.status(
                Role.PLANNER, "working", "replanning with your feedback"
            )
            await self.send(
                MessageSent(
                    from_agent="planner", to_agent="planner", summary="plan refused; revising"
                )
            )
            revised_notes = self._notes(run_id, ()) + (f"plan feedback: {feedback}",)
            # replan with the feedback as a client note, still read-only + one file
            async def replan_tool(name: str, args: dict) -> str:
                return await self.call_tool(run_id, name, args, agent=scope)

            try:
                await self.call_tool(
                    run_id,
                    "claim_scope",
                    {
                        "owned_paths": [PLAN_DOC, CONTRACT_DOC],
                        "task": "replan",
                        "status": "working",
                    },
                    agent=scope,
                )
                graph2 = await self._run_planner(spec, revised_notes, replan_tool)
                await self.write_files(
                    run_id,
                    [WorkFile(path=CONTRACT_DOC, content=render_plan_markdown(spec, graph2))],
                    agent=scope,
                )
            finally:
                await self.call_tool(run_id, "release_scope", {}, agent=scope)
            # one revision is all you get: approve or end
            lines2 = self._plan_lines(spec, graph2, f"revised plan — {len(graph2.tasks)} tasks:")
            try:
                await self.call_tool(
                    run_id, "confirm_plan", {"lines": lines2, "round": 2}, agent=scope
                )
            except ToolDenied as second:
                raise LoopError(f"plan not approved: {second}") from second
            await self.status(Role.PLANNER, "done", "revised plan approved")
            await self.usage_tick()
            return await self._adopt_plan(run_id, graph2)

    def _routing_note(self) -> str | None:
        """One line naming the coding roles on the fallback route, or None.

        Coding roles prefer the open-weight endpoint and fall back to Meta/Spark
        only when it is unset — run-9f836de2 spent 86% of calls on fallback with
        no signal to the user. Surface the lever, not just the cost.
        """
        from kiln.agents.roles import OPEN_WEIGHT_ROLES
        from kiln.gateway.routing import route_for

        fallback_roles = sorted(
            role.value
            for role in OPEN_WEIGHT_ROLES
            if route_for(role, self.settings).fallback
        )
        if not fallback_roles:
            return None
        return (
            f"routing: {', '.join(fallback_roles)} are on the fallback "
            "meta/Spark route (open_weight_base_url is unset) — the cheaper "
            "coding path is one .env entry away"
        )

    async def _phases(self, run_id: str, idea: str, notes: tuple[str, ...]) -> bool:
        self._worker_seq = 0
        self._board = None
        self._failed_tasks.clear()
        self._lanes_tried.clear()
        self._retry_notes.clear()
        self._run_notes.clear()
        self._suite_healed = False
        self._file_requests.clear()
        self._graph = None
        self._contract_files = []
        self._run_id = run_id
        self._idea = idea
        self._added_task_ids.clear()
        self._decisions.clear()
        self._supervisor_calls = 0
        self._shell_failures.clear()
        self._root_state.clear()
        self._root_locks.clear()
        routing_note = self._routing_note()
        if routing_note:
            logger.info("%s", routing_note)
            await self.send(
                MessageSent(from_agent="kiln", to_agent="client", summary=routing_note)
            )
        req_scope = self._scope("requirements", "Riley Requirements", Role.REQUIREMENTS)

        async def req_tool(name: str, args: dict) -> str:
            return await self.call_tool(run_id, name, args, agent=req_scope)

        await self._load_project_skills(run_id)
        self._inventory = await self._take_inventory(run_id)
        if self._inventory:
            logger.info("existing workspace (%d top-level entries)", len(self._inventory))
        self._at("requirements", Role.REQUIREMENTS)
        await self.status(Role.REQUIREMENTS, "thinking", "reading the idea")
        spec = await requirements.run_async(
            self.gateway,
            idea,
            self._notes(run_id, notes),
            call_tool=req_tool,
            ask=self.settings.ask_requirements,
            inventory=self._inventory or None,
        )
        self._spec = spec
        discovery = (await self.read_workspace(run_id, [requirements.DISCOVERY_DOC])).get(
            requirements.DISCOVERY_DOC, ""
        )
        self._ui_product = needs_ui_product_skill(
            idea=idea, discovery=discovery, spec=spec
        )
        self._prod_ops = needs_prod_ops_skill(
            idea=idea, discovery=discovery, spec=spec
        )
        self._security = needs_security_skill(
            idea=idea, discovery=discovery, spec=spec
        )
        self._frameworks = tuple(spec.frameworks or ())
        await self.status(Role.REQUIREMENTS, "done", f"spec: {spec.title}")
        await self.usage_tick()
        await self.send(
            MessageSent(from_agent="requirements", to_agent="planner", summary="spec.md ready")
        )

        self._at("the stack check", Role.REQUIREMENTS)
        await self._resolve_stack(run_id, spec.stack_id)
        profile = profile_for(self.stack_id)

        self._at("planning", Role.PLANNER)
        graph = await self._plan(run_id, spec, notes)

        # deterministic scaffold: the companion runs each root's official
        # recipe before any worker starts, so the scaffold *task* only adapts
        # seams instead of hand-writing a Vite/package.json/FastAPI skeleton.
        self._at("scaffolding", Role.PLANNER)
        await self._scaffold_roots(run_id, spec, graph)
        self._graph = graph
        await self._write_contract_artifacts(run_id, spec, graph)

        self._at("writing the test suite", Role.TEST_AUTHOR)
        await self.status(Role.TEST_AUTHOR, "working", "writing tests from the spec")
        # the suite judges what this run builds, against the names the workers
        # are told to build: in-scope criteria only, and the planner's contract
        in_scope = graph.in_scope([c.id for c in spec.acceptance_criteria])
        suite = await asyncio.to_thread(
            test_author.run,
            self.gateway,
            spec,
            contract=graph.contract,
            in_scope=in_scope if self._deferred else None,
            **self._skill_kwargs(Role.TEST_AUTHOR),
        )
        suite = await self._protect_suite(run_id, suite)
        self._suite = suite
        # the spec named its oracle files before the suite existed: point
        # every criterion at a file that does (the developers read them)
        spec, oracle_notes = test_author.align_oracles(spec, suite)
        if oracle_notes:
            self._spec = spec
            self._run_notes.extend(f"spec oracle: {note}" for note in oracle_notes)
            logger.info("aligned spec oracles with the suite: %s", oracle_notes)
        await self.status(Role.TEST_AUTHOR, "done", f"{len(suite.files)} test files ready")
        await self.usage_tick()
        flow_owned = ["tests/", "docs/"]
        test_scope = self._scope(
            "test_author", "Tessa Tests", Role.TEST_AUTHOR, flow_owned
        )
        await self.call_tool(
            run_id,
            "claim_scope",
            {"owned_paths": flow_owned, "task": "suite", "status": "working"},
            agent=test_scope,
        )
        await self.write_files(run_id, suite.files, agent=test_scope)
        if self._ui_product:
            plan = suite.browser_flows or fallback_flow_plan(
                idea=idea, discovery=discovery
            )
            await self.write_files(
                run_id,
                [
                    WorkFile(
                        path=BROWSER_FLOWS_DOC,
                        content=plan.model_dump_json(indent=2) + "\n",
                    )
                ],
                agent=test_scope,
            )
        await self.call_tool(run_id, "release_scope", {}, agent=test_scope)

        files_by_task = await self._build(run_id, spec, suite, graph, profile.dev_notes)

        if self.settings.sandbox == "docker":
            self._at("vendoring dependencies")
            await self.call_tool(run_id, "vendor_deps", {})

        self._at("the test-and-fix loop", Role.FIXER)
        green, report = await self._test_and_fix(run_id, graph, files_by_task)

        review: ReviewVerdict | None = None
        if green:
            self._at("the product gates", Role.REVIEWER)
            product_gaps = await self._product_gates(run_id, idea, graph)
            if product_gaps:
                green, report = await self._test_and_fix(
                    run_id,
                    graph,
                    files_by_task,
                    brief_gaps=product_gaps,
                )
            if green:
                self._at("the brief review", Role.REVIEWER)
                review = await self._review(run_id, idea, spec, graph, report)
                if review.gaps:
                    # only evidence-backed gaps reopen the fixer — one bounded
                    # round; the fix cap inside _test_and_fix still applies
                    green, report = await self._test_and_fix(
                        run_id,
                        graph,
                        files_by_task,
                        brief_gaps=[
                            f"{gap.claim} (brief-review): {gap.evidence}"
                            for gap in review.gaps
                        ],
                    )
                    if green:
                        # Re-verify: a green suite after the patch does not prove
                        # the brief gaps closed (the suite never covered them).
                        # run-9f836de2 would have shipped an unmounted app had the
                        # fixer's green not been re-checked. One bounded re-review.
                        recheck = await self._review(run_id, idea, spec, graph, report)
                        if recheck.gaps:
                            logger.warning(
                                "brief re-review still found %d gap(s) after the fix "
                                "round; marking the run not green",
                                len(recheck.gaps),
                            )
                            await self.status(
                                Role.REVIEWER,
                                "stuck",
                                f"{len(recheck.gaps)} brief gap(s) still open after the fix",
                            )
                            review = recheck
                            green = False
                        else:
                            review = recheck

        # A UI product whose browser gate could not run on this host was never
        # actually verified. BLOCKED_HOST_SETUP is not a fixer-fixable code gap,
        # so it does not reopen the fixer — it vetoes the green verdict outright
        # (run-9f836de2 silently skipped the browser and shipped an unmounted UI).
        if green and self._ui_product and self._blocked_gates:
            logger.warning(
                "UI run not green: %d product gate(s) blocked by host setup: %s",
                len(self._blocked_gates),
                "; ".join(self._blocked_gates),
            )
            await self.status(
                Role.REVIEWER,
                "stuck",
                "UI not verified — browser gate blocked on this host "
                "(install Playwright/Chromium and re-run to confirm green)",
            )
            green = False

        # The reporter stage is documentation. A failure here — an invalid
        # bundle, a write the ledger refuses, a companion hiccup — is a defect
        # to surface, never a reason to flip a run the suite already decided.
        # run-9f836de2 burned 50 green minutes on a docs-write ExecutionError.
        self._at("writing the docs", Role.REPORTER)
        await self.status(Role.REPORTER, "working", "writing README and cost report")
        reporter_scope = self._scope(
            "reporter",
            "Rosa Reporter",
            Role.REPORTER,
            ["README.md", "AGENTS.md", "docs/"],
        )
        claimed = False
        try:
            if self.settings.agentic_devs:
                # agentic reporter: read-only verification (glob/grep/read) before
                # it commits claims to the documents
                async def report_tool(name: str, args: dict) -> str:
                    return await self.call_tool(
                        run_id, name, args, agent=reporter_scope, record=False
                    )

                bundle = await reporter.run_agentic(
                    self.gateway,
                    spec,
                    graph,
                    self.worker_results,
                    report,
                    self.gateway.usage.summary(),
                    report_tool,
                    transcript=self.transcript,
                    agent_id="reporter",
                    review=review,
                    run_notes=list(self._run_notes),
                )
            else:
                bundle = await asyncio.to_thread(
                    reporter.run,
                    self.gateway,
                    spec,
                    graph,
                    self.worker_results,
                    report,
                    self.gateway.usage.summary(),
                    review=review,
                    run_notes=list(self._run_notes),
                )
            await self.call_tool(
                run_id,
                "claim_scope",
                {
                    "owned_paths": ["README.md", "AGENTS.md", "docs/"],
                    "task": "report",
                    "status": "working",
                },
                agent=reporter_scope,
            )
            claimed = True
            # in an existing project the user's README / AGENTS.md are theirs:
            # Kiln's versions land under docs/ instead of overwriting them
            report_files = [
                WorkFile(
                    path=await self._protected_target(run_id, "README.md"),
                    content=bundle.readme,
                ),
                WorkFile(path="docs/run-summary.md", content=bundle.run_summary),
                WorkFile(path="docs/cost-report.md", content=bundle.cost_report),
            ]
            if (bundle.agents_md or "").strip():
                report_files.append(
                    WorkFile(
                        path=await self._protected_target(run_id, "AGENTS.md"),
                        content=bundle.agents_md,
                    )
                )
            await self.write_files(run_id, report_files, agent=reporter_scope)
        except Exception as exc:  # noqa: BLE001 — docs failure must not fail the run
            logger.warning("reporter stage failed; run result stands (green=%s): %s", green, exc)
            await self.status(
                Role.REPORTER,
                "stuck",
                f"docs failed (run result stands): {str(exc)[:120]}",
            )
            await self.usage_tick()
            return green
        finally:
            if claimed:
                try:
                    await self.call_tool(run_id, "release_scope", {}, agent=reporter_scope)
                except Exception:  # noqa: BLE001 — releasing a lease is best-effort
                    logger.warning("reporter release_scope failed", exc_info=True)
        await self.status(Role.REPORTER, "done", "docs ready")
        await self.usage_tick()
        return green

    # ── the two developers ──────────────────────────────────────────────────

    def _team(self, graph: TaskGraph) -> TeamBoard:
        """The team board for this run: two developers (one when configured so)."""
        if self._board is None:
            self._board = TeamBoard(graph, self._spec, self._lane_count())
            self._board.listener = self._on_board_message
        return self._board

    async def _build(
        self,
        run_id: str,
        spec,
        suite: TestSuite,
        graph: TaskGraph,
        stack_notes: str,
    ) -> dict[str, dict[str, str]]:
        """Build every task with the two developers, in one scheduling pass.

        A scaffold installs its root's dependencies and is smoke-checked (one
        repair pass on failure) before it counts as done, so the tasks on that
        root start straight after it — never behind the other root's scaffold.
        When every scaffold has settled the suite runs once for the test state.
        Each developer takes the next ready task from its own queue, an idle
        developer steals a ready task from a busy teammate, and the suite
        refreshes whenever both are between tasks. No single failure ends the
        build: failed tasks are retried, failed over, then recorded.
        """
        self._building = True
        try:
            return await self._build_stages(run_id, spec, suite, graph, stack_notes)
        finally:
            self._building = False

    async def _build_stages(
        self,
        run_id: str,
        spec,
        suite: TestSuite,
        graph: TaskGraph,
        stack_notes: str,
    ) -> dict[str, dict[str, str]]:
        files_by_task: dict[str, dict[str, str]] = {}
        self._team(graph)
        await self._publish_team()
        self._at("the scaffold tasks")
        # one scheduling pass, no stage barrier: see _run_tasks(prepare=True)
        await self._run_tasks(
            run_id, list(graph.tasks), spec, suite, files_by_task, graph, stack_notes,
            state=_SuiteState(), smoke=True, prepare=True,
        )
        return files_by_task

    async def _run_tasks(
        self,
        run_id: str,
        tasks: list[Task],
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
        *,
        state: "_SuiteState | None",
        smoke: bool,
        prepare: bool = False,
    ) -> None:
        """Schedule ``tasks`` across the developers until each is done or failed.

        ``prepare`` (the real build): there is no barrier between the scaffold
        layer and the rest. A layer-0 task installs its root's dependencies
        and is smoke-checked inside its developer's job, and only then counts
        as done — so the next task on that root starts at once, while the
        other developer is still on its own scaffold (run-aa748f20's Dev B sat
        idle ten minutes behind Dev A's scaffold and two serial installs).
        Once all of layer 0 has settled, any root no task installed (the node
        test runner at the workspace root) is installed and the suite runs
        once, so later tasks get a test state.
        """
        board = self._team(graph)
        for task in tasks:
            board.ensure(task)
        subset = {task.id for task in tasks}
        running: dict[asyncio.Task, tuple[str, str]] = {}
        landed_since_suite = 0
        first_ids = {task.id for task in dependency_layers(graph)[0]} & subset if prepare else set()
        layer0_settled = not prepare

        def ready(tid: str, key: str) -> bool:
            # a failed dependency settles too: the dependent is told and stubs
            # what it needs instead of the whole build stopping behind it
            settled = all(
                dep not in board.tasks or board.tasks[dep].status in ("done", "failed")
                for dep in board.tasks[tid].task.depends_on
            )
            if prepare and tid not in first_ids and not self._root_ready(spec, board.tasks[tid].task):
                return False  # its root's dependencies are not installed yet
            # a task whose files the teammate has checked out waits for them
            return settled and not board.blocked_by_borrow(tid, key)

        try:
            while True:
                if not layer0_settled and all(
                    tid not in board.tasks or board.tasks[tid].status in ("done", "failed")
                    for tid in first_ids
                ):
                    layer0_settled = True
                    if self.settings.sandbox != "docker":
                        self._at("installing the remaining dependencies")
                        await self._install_remaining_roots(run_id, spec)
                    more = any(
                        board.tasks[tid].status == "pending" for tid in board.order if tid in subset
                    )
                    if state is not None and more:
                        self._at("the test run after the scaffold tasks")
                        await self._refresh_suite_state(run_id, state)
                        landed_since_suite = 0
                    self._at("the feature tasks")
                if state is not None:
                    # the feature stage also builds tasks the lead adds mid-build
                    subset |= {tid for tid in self._added_task_ids if tid in board.tasks}
                pending = [
                    tid for tid in board.order if tid in subset and board.tasks[tid].status == "pending"
                ]
                if not pending and not running:
                    if state is not None and any(not job.done() for job in self._supervisor_jobs):
                        await self._await_lead()  # its decision may add work
                        continue
                    break
                if state is not None and not running and pending and landed_since_suite:
                    await self._refresh_suite_state(run_id, state)
                    landed_since_suite = 0
                busy = {lane for lane, _ in running.values()}
                for key in board.lanes:
                    if key in busy:
                        continue
                    tid = next((t for t in board.queue(key, subset) if ready(t, key)), None)
                    teammate = board.other(key)
                    if tid is None and teammate is not None and teammate.key in busy:
                        # work stealing: take the ready task the busy teammate
                        # would reach last, so its own next task stays with it
                        stealable = [t for t in board.queue(teammate.key, subset) if ready(t, key)]
                        tid = stealable[-1] if stealable else None
                    if tid is None:
                        continue
                    board.start(tid, key)
                    busy.add(key)
                    tried = self._lanes_tried.get(tid, set())
                    job = asyncio.create_task(
                        self._task_job(
                            run_id,
                            board.tasks[tid].task,
                            spec,
                            suite,
                            files_by_task,
                            graph,
                            stack_notes,
                            state.block() if state is not None else "",
                            lane_key=key,
                            retry_note=self._retry_notes.pop(tid, ""),
                            retries=0 if tried else None,
                            settle=prepare,
                            install_root=prepare and tid in first_ids,
                            smoke=smoke,
                        )
                    )
                    running[job] = (key, tid)
                    await self._publish_team()
                if not running and any(not job.done() for job in self._supervisor_jobs):
                    # nothing can start, but the lead is deciding: its plan
                    # change may unblock the remaining tasks
                    await self._await_lead()
                    if not any(not job.done() for job in self._supervisor_jobs):
                        continue  # re-plan with whatever the decision changed
                if not running:
                    # nothing can start and nothing is running: the remaining
                    # tasks wait on something that will never settle
                    for tid in pending:
                        self._record_failure(tid, board.tasks[tid].lane, "its dependencies never settled")
                    break
                finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for job in finished:
                    key, tid = running.pop(job)
                    try:
                        result = job.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — failover, then recorded
                        await self._handle_task_failure(tid, key, exc)
                        await self._publish_team()
                        continue
                    if prepare:
                        # landed, installed and smoke-checked inside the job;
                        # the board hears "done" only now, so no dependent
                        # started against an uninstalled or broken root
                        latest = self._results_by_task.get(tid, result)
                        board.finish(
                            tid, key, summary=latest.summary,
                            files=list(files_by_task.get(tid, {})), gaps=list(latest.known_gaps),
                        )
                        await self._publish_team()
                        landed_since_suite += 1
                        continue
                    await self._land(run_id, result, files_by_task, lane_key=key)
                    await self._publish_team()
                    landed_since_suite += 1
                    if smoke:
                        await self._smoke_or_repair(
                            run_id, board.tasks[tid].task, spec, suite, files_by_task, graph, stack_notes
                        )
        finally:
            # never leave a developer running detached while the run tears down
            for job in running:
                job.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    async def _task_job(
        self,
        run_id: str,
        task: Task,
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
        test_state: str,
        *,
        lane_key: str,
        retry_note: str,
        retries: int | None,
        settle: bool,
        install_root: bool,
        smoke: bool,
    ) -> WorkerResult:
        """One developer's job: the task, then (``settle``) everything that
        must happen before it counts as done — recorded, its root installed
        (layer 0), smoke-checked with one repair pass. It all runs in the
        developer's own slot, so the teammate keeps building meanwhile."""
        result = await self._run_task(
            run_id, task, spec, suite, files_by_task, graph, stack_notes, test_state,
            lane_key=lane_key, retry_note=retry_note, retries=retries,
        )
        if not settle:
            return result
        await self._land(run_id, result, files_by_task)  # on record; the board is told later
        try:
            if install_root and self.settings.sandbox != "docker":
                await self._install_root_of(run_id, spec, task)
            if smoke:
                await self._smoke_or_repair(
                    run_id, self._board.tasks[task.id].task, spec, suite, files_by_task, graph, stack_notes
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the task is delivered; checks are advisory here
            logger.warning("post-task checks for %s failed: %s", task.id, exc)
            self._run_notes.append(f"task {task.id}: checks after delivery failed: {_first_lines(str(exc), limit=200)}")
        return result

    # ── dependencies, one root at a time ────────────────────────────────────

    def _install_targets(self, spec) -> dict[str, str]:
        """Root path → stack for every root Kiln installs ('.' for a single-root app)."""
        from kiln.schemas.spec import UI_FRAMEWORKS

        roots = [r for r in (spec.roots or []) if r.framework] if spec is not None else []
        if not roots:
            return {".": self.stack_id}
        return {
            (r.path.strip("/") or "."): (
                "node" if r.framework in UI_FRAMEWORKS or r.framework in ("express", "nestjs") else "python"
            )
            for r in roots
        }

    def _task_root(self, spec, task: Task) -> str | None:
        targets = self._install_targets(spec)
        root = (task.root or "").strip("/") or "."
        if root in targets:
            return root
        # a task under a declared root's folder belongs to it
        for path in targets:
            if path != "." and any(p == path or p.startswith(path + "/") for p in task.owned_paths):
                return path
        return "." if "." in targets else None

    def _root_ready(self, spec, task: Task) -> bool:
        if self.settings.sandbox == "docker":
            return True
        root = self._task_root(spec, task)
        return root is None or root in self._root_state

    async def _install_root_of(self, run_id: str, spec, task: Task) -> None:
        root = self._task_root(spec, task)
        if root is not None:
            await self._install_root(run_id, spec, root)

    async def _install_root(self, run_id: str, spec, root: str, stack: str | None = None) -> None:
        """Install one root once (concurrent callers wait for the first).

        A failure is recorded, never raised: the suite's self-heal and the
        fixer still get their turn, and nothing waits on the root forever.
        """
        lock = self._root_locks.setdefault(root, asyncio.Lock())
        async with lock:
            if root in self._root_state:
                return
            targets = self._install_targets(spec)
            stack = stack or targets.get(root, self.stack_id)
            declared = [r for r in (spec.roots or []) if r.framework] if spec is not None else []
            try:
                if not declared:
                    await self.call_tool(run_id, "install_deps", {"stack_id": stack})
                else:
                    await self.call_tool(
                        run_id, "install_deps", {"stack_id": stack, "root": root},
                        agent=self._scope("orchestrator", "Kiln", Role.PLANNER),
                    )
                self._root_state[root] = "installed"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — recorded
                self._root_state[root] = "failed"
                logger.warning("install_deps(%s @ %s) failed: %s", stack, root, exc)
                self._run_notes.append(
                    f"installing the {stack} dependencies at {root} failed: {_first_lines(str(exc), limit=200)}"
                )

    async def _install_remaining_roots(self, run_id: str, spec) -> None:
        """Roots no layer-0 task installed, plus a node suite's runner at the root."""
        for root, stack in self._install_targets(spec).items():
            await self._install_root(run_id, spec, root, stack)
        if self.stack_id == "node" and "." not in self._install_targets(spec):
            # a split node product's suite runs at the workspace root, which no
            # root declares: install its runner there explicitly
            await self._install_root(run_id, spec, ".", "node")

    async def _await_lead(self) -> None:
        """Let in-flight lead decisions land (bounded by the escalation wait)."""
        pending = [job for job in self._supervisor_jobs if not job.done()]
        if pending:
            await asyncio.wait(pending, timeout=self.settings.supervisor_wait_seconds)

    async def _handle_task_failure(self, tid: str, key: str, exc: BaseException) -> None:
        """Fail over to the teammate once; after that the task is recorded as failed."""
        board = self._board
        reason = _first_lines(str(exc), limit=300)
        tried = self._lanes_tried.setdefault(tid, set())
        tried.add(key)
        teammate = board.other(key)
        if self.settings.worker_failover and teammate is not None and teammate.key not in tried:
            board.reassign(tid, teammate.key)
            board.release(key)
            label = board.lanes[key].label
            self._retry_notes[tid] = f"{label} could not deliver this task ({reason})"
            board.post(None, teammate.key, f"{tid} is now yours: {label} could not deliver it", category="status")
            logger.warning("%s failed %s; failing over to %s", label, tid, board.lanes[teammate.key].label)
            return
        self._record_failure(tid, key, reason)

    def _record_failure(self, tid: str, key: str, reason: str) -> None:
        board = self._board
        board.fail(tid, key, reason)
        entry = board.tasks[tid]
        title = entry.task.title
        self._failed_tasks[tid] = reason
        self._run_notes.append(f"task {tid} ({title}) was not delivered: {reason}")
        logger.warning("task %s not delivered: %s", tid, reason)
        if self._building and self.settings.enable_supervisor:
            # the lead may split it or add a replacement so the build goes on
            escalation = Escalation(
                kind="plan",
                summary=f"task {tid} ({title}) was not delivered after a retry and a failover"[:400],
                details=(
                    f"reason: {reason[:1000]}\nowned paths: {', '.join(entry.task.owned_paths)[:1000]}\n"
                    f"description: {entry.task.description[:1500]}"
                ),
                source="kiln",
                task_id=tid,
            )
            self._escalate_soon(escalation, reply_lane=key)

    async def _land(
        self,
        run_id: str,
        result: WorkerResult,
        files_by_task: dict[str, dict[str, str]],
        lane_key: str | None = None,
    ) -> None:
        """Record one finished task (and, in one-shot mode, write its files)."""
        previous = self._results_by_task.get(result.task_id)
        if previous is not None and previous in self.worker_results:
            # a repair pass replaces the task's earlier result, not duplicates it
            self.worker_results[self.worker_results.index(previous)] = result
        else:
            self.worker_results.append(result)
        self._results_by_task[result.task_id] = result
        if not self.settings.agentic_devs:
            await self.write_files(run_id, result.files)
        else:
            self._note_written(file.path for file in result.files)
        files = files_by_task.setdefault(result.task_id, {})
        files.update({file.path: file.content for file in result.files})
        board = self._board
        if board is not None and result.task_id in board.tasks:
            if lane_key is not None:
                board.finish(
                    result.task_id,
                    lane_key,
                    summary=result.summary,
                    files=list(files),
                    gaps=list(result.known_gaps),
                )
            else:
                board.tasks[result.task_id].files = list(files)[:20]

    async def _smoke(self, run_id: str, paths: list[str], where: str) -> None:
        if not paths:
            return
        try:
            await self.call_tool(
                run_id, "smoke_check", {"stack_id": self.stack_id, "paths": paths}
            )
        except ToolDenied:
            pass  # the user declined; the real suite still decides green
        except Exception as exc:
            raise LoopError(f"smoke check failed after {where}: {exc}") from exc

    async def _smoke_or_repair(
        self,
        run_id: str,
        task: Task,
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
    ) -> None:
        """Smoke-check a delivered task; on failure its developer gets one repair.

        A syntax or import error used to end the run on the spot. Now the
        developer who wrote the files gets one short pass to fix them; if the
        check still fails, the failure is recorded and the build continues —
        the suite and the fixer judge it later.
        """
        paths = list(files_by_task.get(task.id, {}))
        try:
            await self._smoke(run_id, paths, f"task {task.id}")
            return
        except LoopError as exc:
            error = _first_lines(str(exc), lines=8, limit=600)
        board = self._board
        key = board.tasks[task.id].lane if board and task.id in board.tasks else None
        repair = task.model_copy(
            update={
                "description": task.description
                + "\n\nREPAIR PASS: the smoke check (syntax / import / type check) failed "
                f"after this task: {error}\nFix those errors in your owned files only; "
                "do not redo finished work."
            }
        )
        try:
            result = await self._run_task(
                run_id,
                repair,
                spec,
                suite,
                files_by_task,
                graph,
                stack_notes,
                "",
                lane_key=key,
                retries=0,
                max_steps=12,
                track=False,
            )
            await self._land(run_id, result, files_by_task)
        except Exception as exc:  # noqa: BLE001 — recorded below
            logger.warning("repair pass for %s failed: %s", task.id, exc)
        paths = list(files_by_task.get(task.id, {}))
        try:
            await self._smoke(run_id, paths, f"task {task.id} (after a repair pass)")
            self._run_notes.append(f"task {task.id}: smoke check failed once, fixed by a repair pass")
        except LoopError as exc:
            self._run_notes.append(
                f"task {task.id}: smoke check still fails after one repair pass "
                f"({_first_lines(str(exc), limit=300)}); left to the suite and the fixer"
            )

    async def _run_suite(self, run_id: str) -> TestReport | None:
        """Run the suite; if it cannot run, heal the toolchain once and retry.

        Returns None when the suite still cannot run — callers carry on (a
        missing mid-build state, or a final "not green") instead of crashing.
        """
        try:
            return await self.run_tests(run_id)
        except ToolDenied as exc:
            self._note_once(f"the test run was declined ({exc}); the suite did not run")
            return None
        except Exception as exc:  # noqa: BLE001 — healed below
            failure: Exception = exc
        if not self._suite_healed:
            self._suite_healed = True
            logger.warning("suite could not run (%s); reinstalling the suite toolchain", failure)
            try:
                await self.call_tool(
                    run_id, "install_deps", {"stack_id": self.stack_id, "root": "."}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("toolchain reinstall failed: %s", exc)
            try:
                report = await self.run_tests(run_id)
                self._note_once("the suite could not run once; reinstalling its toolchain fixed it")
                return report
            except Exception as exc:  # noqa: BLE001
                failure = exc
        self._note_once(f"the test suite could not run: {_first_lines(str(failure), limit=300)}")
        return None

    def _note_once(self, note: str) -> None:
        if note not in self._run_notes:
            self._run_notes.append(note)

    async def _refresh_suite_state(self, run_id: str, state: "_SuiteState") -> None:
        """Run the suite and fold the result into the regression state.

        The suite covers the whole in-scope spec, so red mid-build is expected;
        the green list is what later tasks must not break.
        """
        report = await self._run_suite(run_id)
        if report is None:
            return
        await self.send(
            TestRunFinished(
                passed=report.passed,
                failed=report.failed,
                skipped=report.skipped,
                errors=report.errors,
                green=report.green,
            )
        )
        green_now = [case.nodeid for case in report.cases if case.outcome == "passed"]
        broken_now = {
            case.nodeid for case in report.cases if case.outcome in ("failed", "error")
        }
        state.regressions = [nodeid for nodeid in state.known_green if nodeid in broken_now]
        state.known_green = green_now
        state.known_broken = len(broken_now)

    async def _run_task(
        self,
        run_id: str,
        task: Task,
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
        test_state: str,
        *,
        lane_key: str | None = None,
        retry_note: str = "",
        retries: int | None = None,
        max_steps: int | None = None,
        track: bool = True,
    ) -> WorkerResult:
        """One task on one developer, with a fresh conversation per attempt.

        A developer that wrote in-scope files returns a salvaged partial result
        and never lands here as a failure; a failure means nothing was
        delivered, so a fresh attempt (told why) is cheap insurance against one
        bad trajectory. Raises LoopError when every attempt fails — the
        scheduler then fails over to the teammate.
        """
        board = self._team(graph)
        board.ensure(task)
        key = lane_key or board.tasks[task.id].lane
        lane = board.lanes[key]
        if track and board.tasks[task.id].status != "running":
            board.start(task.id, key)
        ref = AgentRef(id=lane.agent_id, role=Role.WORKER, label=lane.label)
        attempts = 1 + (self.settings.worker_retries if retries is None else retries)
        last: Exception | None = None
        note = retry_note
        for attempt in range(1, attempts + 1):
            self._worker_seq += 1
            try:
                return await self._attempt_task(
                    run_id,
                    task,
                    ref,
                    key,
                    spec,
                    suite,
                    files_by_task,
                    graph,
                    stack_notes,
                    test_state,
                    note,
                    max_steps,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced
                last = exc
                note = str(exc)[:300]
                await self.send(AgentStatus(agent=ref, status="stuck", detail=str(exc)[:200]))
                if attempt < attempts:
                    logger.warning("%s (%s) failed; retrying with a fresh conversation: %s", ref.id, task.id, exc)
        assert last is not None
        suffix = f" after {attempts} attempts" if attempts > 1 else ""
        raise LoopError(f"{ref.id} ({task.title}) failed{suffix}: {last}") from last

    def _handoffs_for(self, task: Task) -> list[dict]:
        handoffs = []
        for dep in task.depends_on:
            if dep in self._results_by_task:
                result = self._results_by_task[dep]
                handoffs.append(
                    {
                        "task_id": dep,
                        "summary": result.summary,
                        "what_works": result.what_works,
                        "known_gaps": list(result.known_gaps),
                    }
                )
            elif dep in self._failed_tasks:
                handoffs.append(
                    {
                        "task_id": dep,
                        "summary": "NOT DELIVERED — this dependency failed, so its files may be "
                        "missing or partial. Code against the contract, stub what you need "
                        "inside your own owned paths, and record the gap.",
                        "what_works": "",
                        "known_gaps": [self._failed_tasks[dep]],
                    }
                )
        return handoffs

    async def _attempt_task(
        self,
        run_id: str,
        task: Task,
        ref: AgentRef,
        key: str,
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
        test_state: str,
        retry_note: str,
        max_steps: int | None = None,
    ) -> WorkerResult:
        board = self._board
        teammate = board.other(key)
        await self.send(
            TaskAssigned(
                task=TaskRef(id=task.id, title=task.title, owned_paths=task.owned_paths),
                agent_id=ref.id,
            )
        )
        await self.send(AgentStatus(agent=ref, status="working", detail=f"writing {task.title}"))
        self._task_agent[task.id] = ref.id
        scope = self._scope(ref.id, ref.label, Role.WORKER, task.owned_paths)
        await self.call_tool(
            run_id,
            "claim_scope",
            {"owned_paths": task.owned_paths, "task": task.title, "status": "working"},
            agent=scope,
        )
        board.lanes[key].current = task.id

        async def extend_lease() -> None:
            await self.call_tool(
                run_id,
                "claim_scope",
                {
                    "owned_paths": [*task.owned_paths, *board.borrowed_paths(key)],
                    "task": task.title,
                    "status": "working",
                },
                agent=scope,
            )

        async def request_files(paths: list[str], reason: str, wait: bool) -> str:
            return await self._request_files(key, task, paths, reason, wait, extend_lease)
        current: dict[str, str] = {}
        for dependency in task.depends_on:
            current.update(files_by_task.get(dependency, {}))

        async def call_tool(name: str, args: dict) -> str:
            # run_agent_loop records these calls; do not double them
            output = await self.call_tool(run_id, name, args, agent=scope, record=False)
            if name != "shell":
                return output
            if teammate is not None and _LEAK_MARKER in (output or ""):
                changed = output.split(_LEAK_MARKER, 1)[1].split("]", 1)[0].strip()
                board.post(
                    key,
                    teammate.key,
                    f"a shell command of mine changed files outside my lease: {changed} — "
                    "re-read them before you edit",
                )
            found = shell_hint(output or "")
            if found is None:
                return output
            kind, hint = found
            output = f"{output}\n[hint: {hint}]"
            count_key = (key, task.id, kind)
            self._shell_failures[count_key] = self._shell_failures.get(count_key, 0) + 1
            if self._shell_failures[count_key] == 2 and self.settings.enable_supervisor:
                # the same kind of failure again: the lead takes a look, while
                # the developer keeps going with the hint
                self._escalate_soon(
                    Escalation(
                        kind="environment",
                        summary=f"{ref.label} hit a recurring shell failure ({kind}) on {task.id}"[:400],
                        details=f"command: {str(args.get('command', ''))[:1000]}\noutput:\n{(output or '')[-2500:]}",
                        source=ref.id,
                        task_id=task.id,
                    ),
                    reply_lane=key,
                )
                output += "\n[the tech lead has been told; keep going — its advice arrives as a message]"
            return output

        async def escalate(payload) -> str:
            await self.send(
                AgentStatus(agent=ref, status="stuck", detail=f"asked the tech lead: {payload.summary[:100]}")
            )
            reply = await self._escalate(
                Escalation(
                    kind=payload.kind,
                    summary=payload.summary,
                    details=payload.details,
                    source=ref.id,
                    task_id=task.id,
                ),
                wait=payload.wait,
                reply_lane=key,
            )
            await self.send(AgentStatus(agent=ref, status="working", detail=f"back on {task.title}"))
            return reply

        async def notify(message: str, reply_to: str = "") -> str:
            if teammate is None:
                return "no teammate"
            if reply_to:
                return board.answer(key, reply_to, message)
            return board.post(key, teammate.key, message)

        async def ask(question: str, wait: bool) -> str:
            return await self._ask_peer(key, question, wait)

        async def propose(payload) -> str:
            return await self._propose_contract_change(run_id, key, payload)

        async def pre_submit(paths: list[str]) -> str | None:
            return await self._contract_check(paths, call_tool)

        briefing = await self._product_briefing(run_id)
        skills = self._skill_kwargs(Role.WORKER, tuple(task.required_skills), root=task.root)
        team_context = board.render_for(key, task.id)
        contract_brief = self._contract_brief()
        if contract_brief:
            team_context = f"{contract_brief}\n\n{team_context}"
        handoffs = self._handoffs_for(task)
        if teammate is not None:
            # Two developers write code that must read as one: both follow the
            # same framework and engineering rules, so those packs are required
            # (still loaded on demand) rather than merely recommended.
            shared = [
                name
                for name in (*frameworks_for_root(self._spec, task.root), "eng_quality")
                if name in SKILL_CATALOG
            ]
            required = list(dict.fromkeys([*task.required_skills, *shared]))
            task = task.model_copy(update={"required_skills": required})
        try:
            if self.settings.agentic_devs:
                # agentic mode passes references, not blobs: the files are on
                # the user's disk and the developer has read_file
                result = await worker.run_agentic(
                    self.gateway,
                    task,
                    spec,
                    wrap_call_tool_with_skills(call_tool),
                    suite=suite,
                    current_files=None,
                    dependency_paths=list(current),
                    contract=graph.contract,
                    environment=self._env(run_id),
                    stack_notes=stack_notes,
                    transcript=self.transcript,
                    agent_id=ref.id,
                    test_state=test_state,
                    design=briefing["design"],
                    discovery=briefing["discovery"],
                    plan_memo=self._plan_memo,
                    handoffs=handoffs,
                    retry_note=retry_note,
                    team_context=team_context,
                    inbox=lambda: board.drain(key),
                    on_notify=notify if teammate is not None else None,
                    on_request_files=request_files,
                    extra_paths=lambda: board.borrowed_paths(key),
                    on_ask=ask if teammate is not None else None,
                    on_propose=propose if graph.contract is not None else None,
                    pre_submit=pre_submit if graph.contract is not None else None,
                    on_escalate=escalate if self.settings.enable_supervisor else None,
                    max_steps=max_steps,
                    **skills,
                )
            else:
                result = await asyncio.to_thread(
                    worker.run,
                    self.gateway,
                    task,
                    spec,
                    suite,
                    current,
                    graph.contract,
                    test_state=test_state,
                    design=briefing["design"],
                    discovery=briefing["discovery"],
                    plan_memo=self._plan_memo,
                    handoffs=handoffs,
                    team_context=team_context,
                    **skills,
                )
        finally:
            await self.call_tool(run_id, "release_scope", {}, agent=scope)
            await self._after_release(key)

        await self.send(AgentStatus(agent=ref, status="done", detail=result.summary[:80]))
        await self.usage_tick()
        return result

    # ── shared files: exclusive checkout with a release notification ──────

    async def _request_files(
        self,
        key: str,
        task: Task,
        paths: list[str],
        reason: str,
        wait: bool,
        extend: Callable[[], Awaitable[None]],
    ) -> str:
        """Grant free files now; queue files the teammate holds.

        The way real teams handle a shared file: whoever is working in it keeps
        it; the next developer either carries on with other work and is told
        the moment it is released, or — when nothing else can move — waits for
        it (a wait costs no tokens: the developer's loop is simply paused).
        Two developers can never both wait on each other.
        """
        board = self._board
        refused: list[str] = []
        wanted: list[str] = []
        for raw in paths:
            try:
                path = safe_relative_path(raw.strip().replace("\\", "/"))
            except Exception:  # noqa: BLE001
                refused.append(f"{raw} (not a workspace path)")
                continue
            if path.startswith("tests/"):
                refused.append(f"{path} (the suite is never edited)")
                continue
            if board.owner_task(path) is None and (
                path in ("README.md", "AGENTS.md") or path.startswith("docs/")
            ):
                refused.append(f"{path} (Kiln's own documents)")
                continue
            mine = [*task.owned_paths, *board.borrowed_paths(key)]
            if any(paths_conflict(path, owned) for owned in mine):
                continue  # already yours
            if path not in wanted:
                wanted.append(path)
        lines: list[str] = []
        if refused:
            lines.append("refused: " + "; ".join(refused))
        free = [p for p in wanted if board.holder(p, key) is None]
        held = [p for p in wanted if p not in free]
        if free:
            problem = await self._grant(key, free, reason, extend)
            lines.append(
                f"granted: {', '.join(free)} — read each one before you edit it"
                if not problem
                else f"could not grant {', '.join(free)}: {problem}"
            )
        if not held:
            return "\n".join(lines) or "nothing to request: those files are already yours"
        holder = board.holder(held[0], key)
        what = f"{', '.join(held)} ({holder.label} is working on {holder.current})"
        if not wait:
            self._file_requests.append(_FileRequest(key, held, reason, extend, notify=True))
            board.post(key, holder.key, f"I need {', '.join(held)} after you ({reason})", category="checkout")
            lines.append(
                f"queued: {what}. Keep working on your other files; you will get a "
                "message the moment they are yours. If nothing else in your task can "
                "move, call request_files again with wait=true."
            )
            return "\n".join(lines)
        mine = [*task.owned_paths, *board.borrowed_paths(key)]
        if board.is_waiting_on(holder.key, key) or any(
            paths_conflict(p, m) for p in board.waiting[holder.key] for m in mine
        ):
            lines.append(
                f"cannot wait: {holder.label} is waiting on your files, so you would both "
                "stop. Finish or release what you hold first: complete your other work, "
                "submit_result with a known_gap for the shared change, and notify_peer them."
            )
            return "\n".join(lines)
        request = _FileRequest(key, held, reason, extend, notify=False)
        self._file_requests.append(request)
        board.waiting[key].update(held)
        board.post(key, holder.key, f"I am paused until you release {', '.join(held)} ({reason})", category="checkout")
        try:
            await asyncio.wait_for(request.event.wait(), timeout=self.settings.file_wait_seconds)
        except TimeoutError:
            if request in self._file_requests:
                self._file_requests.remove(request)
        finally:
            board.waiting[key].difference_update(held)
        if request.granted:
            lines.append(f"now yours: {', '.join(held)} — read each one before you edit it")
        else:
            lines.append(
                f"still held after {self.settings.file_wait_seconds}s: {what}. Finish what "
                "you can and record the shared change as a known_gap."
            )
        return "\n".join(lines)

    # ── questions, contract changes, the contract check ─────────────────────

    async def _ask_peer(self, key: str, question: str, wait: bool) -> str:
        """Ask the teammate; optionally pause (no tokens) until it answers."""
        board = self._board
        teammate = board.other(key)
        if teammate is None:
            return "no teammate on this run — read the contract, shared types and files"
        qid = board.ask(key, teammate.key, question)
        if not wait:
            return (
                f"asked {qid}; keep working — the answer arrives as a message "
                "(meanwhile check the contract, shared types and their files)"
            )
        if teammate.current is None:
            return (
                f"asked {qid}, but {teammate.label} is between tasks, so nobody can answer "
                "now; it is on the board for their next task. Use the contract and their "
                "files meanwhile."
            )
        if board.is_waiting_on(teammate.key, key):
            return (
                f"asked {qid}, but {teammate.label} is paused waiting on you, so you cannot "
                "both wait — answer them or finish first; your answer will arrive as a message."
            )
        entry = board.questions[qid]
        board.awaiting[key] = qid
        try:
            await asyncio.wait_for(entry.event.wait(), timeout=self.settings.peer_answer_wait_seconds)
        except TimeoutError:
            pass
        finally:
            board.awaiting[key] = None
        if entry.answer is not None:
            return f"answer to {qid} from {teammate.label}: {entry.answer}"
        return (
            f"no answer to {qid} yet ({teammate.label} finished or is busy); it stays on the "
            "board. Decide from the contract and their files, and record what you assumed."
        )

    async def _propose_contract_change(self, run_id: str, key: str, payload) -> str:
        """Review, apply and announce one contract change — or refuse it."""
        from kiln.contract import apply_change, change_conflicts

        graph = self._graph
        if graph is None:
            return "Error: no approved contract yet"
        fields = {
            name: None
            if getattr(payload, name) is None
            else [FieldSpec.model_validate(f.model_dump()) for f in getattr(payload, name)]
            for name in ("request_fields", "response_fields")
        }
        async with self._contract_lock:
            try:
                contract, old, new, summary = apply_change(
                    graph.contract,
                    payload.method,
                    payload.path,
                    request_fields=fields["request_fields"],
                    response_fields=fields["response_fields"],
                    status=payload.status,
                )
            except Exception as exc:  # noqa: BLE001
                return f"refused: the change is not a valid route ({_first_lines(str(exc))})"
            vague = new.vagueness()
            if vague:
                return "refused: " + "; ".join(vague)
            suite_text = "\n".join(f.content for f in self._suite.files) if self._suite else ""
            conflicts = change_conflicts(old, new, suite_text)
            if conflicts:
                return (
                    "refused: " + "; ".join(conflicts) + ". The tests were written from the "
                    "contract and are the judge — keep the field, and add what you need instead."
                )
            if summary.endswith("no change"):
                return f"nothing to change: {summary}"
            graph.contract = contract
            await self._publish_contract(run_id, graph)
        board = self._board
        label = board.lanes[key].label if board else "a developer"
        note = f"contract change by {label}: {summary} ({payload.reason})"
        self._run_notes.append(note)
        regenerated = ", ".join(self._contract_files) or "docs/api-contract.json"
        if board is not None:
            teammate = board.other(key)
            if teammate is not None:
                board.post(
                    key,
                    teammate.key,
                    f"CONTRACT CHANGED — {summary} ({payload.reason}). Regenerated: "
                    f"{regenerated}. Update any code that uses this route.",
                    category="contract",
                )
            board.contract_changes += 1
            await self._publish_team()
        return f"accepted: {summary}. Regenerated: docs/contract.md, docs/api-contract.json, {regenerated}"

    async def _publish_contract(self, run_id: str, graph: TaskGraph) -> None:
        """Rewrite docs/contract.md and the generated artifacts after a change."""
        try:
            await self.write_files(
                run_id, [WorkFile(path=CONTRACT_DOC, content=render_plan_markdown(self._spec, graph))]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not rewrite %s: %s", CONTRACT_DOC, exc)
        await self._write_contract_artifacts(run_id, self._spec, graph)

    async def _contract_check(self, paths: list[str], call_tool) -> str | None:
        """Static contract conformance of the files a developer is submitting."""
        from kiln.contract import conformance_gaps

        graph = self._graph
        if graph is None or graph.contract is None or not paths:
            return None
        files: dict[str, str] = {}
        for path in paths[:30]:
            try:
                text = await call_tool("read_file", {"path": path})
            except Exception:  # noqa: BLE001
                continue
            if isinstance(text, str) and text.startswith("# lines "):
                text = text.split("\n", 1)[-1]
            files[path] = text or ""
        gaps = conformance_gaps(files, graph.contract)
        return "; ".join(gaps) if gaps else None

    async def _grant(
        self, key: str, paths: list[str], reason: str, extend: Callable[[], Awaitable[None]]
    ) -> str:
        """Add ``paths`` to ``key``'s lease; '' on success, else why not."""
        board = self._board
        for path in paths:
            board.borrowed[key][path] = reason
        try:
            await extend()
        except Exception as exc:  # noqa: BLE001 — a lease race: undo, report
            for path in paths:
                board.borrowed[key].pop(path, None)
            return _first_lines(str(exc))
        for path in paths:
            owner = board.owner_task(path)
            if owner is not None and owner.lane != key:
                board.post(key, owner.lane, f"I am editing {path} (from {owner.task.id}): {reason}", category="checkout")
        return ""

    async def _after_release(self, key: str) -> None:
        """A developer's lease ended: drop its borrows and hand queued files on."""
        board = self._board
        if board is None:
            return
        board.lanes[key].current = None
        board.clear_borrows(key)
        board.awaiting[key] = None
        board.drop_questions_to(key)
        for request in [r for r in self._file_requests if r.lane == key]:
            self._file_requests.remove(request)
            request.event.set()
        for request in list(self._file_requests):
            if any(board.holder(path, request.lane) is not None for path in request.paths):
                continue
            self._file_requests.remove(request)
            request.granted = not await self._grant(
                request.lane, request.paths, request.reason, request.extend
            )
            if request.notify and request.granted:
                board.post(
                    None,
                    request.lane,
                    f"{', '.join(request.paths)} released and added to your lease — read "
                    "before you edit",
                    category="checkout",
                )
            request.event.set()
        await self._publish_team()

    async def _run_layer(
        self,
        run_id: str,
        layer: list[Task],
        layer_index: int,
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
        test_state: str = "",
    ) -> list[WorkerResult]:
        """Run independent tasks with the developers; returns what was delivered.

        Every developer finishes and releases its lease before this returns; a
        task that could not be delivered is recorded (``_failed_tasks``), not
        raised, so its siblings' work is kept.
        """
        await self._run_tasks(
            run_id, layer, spec, suite, files_by_task, graph, stack_notes, state=None, smoke=False
        )
        return [self._results_by_task[t.id] for t in layer if t.id in self._results_by_task]

    async def _test_and_fix(
        self,
        run_id: str,
        graph: TaskGraph,
        files_by_task: dict[str, dict[str, str]],
        brief_gaps: list[str] | None = None,
    ) -> tuple[bool, TestReport]:
        """Run the suite; patch until green, out of passes, or not converging.

        ``max_fix_attempts`` is the number of fixer passes. The loop also stops
        after FIX_STALL_LIMIT passes in a row that did not lower the failure
        count. The pass that follows a stall — or the last allowed pass — runs
        on the strongest configured route, since a repeat on the same model
        rarely does better.
        """
        max_passes = definition_for(Role.FIXER).max_fix_attempts
        passes = 0
        stalls = 0
        last_failing: int | None = None
        attempt_history: list[str] = []  # what each patch diagnosed, for pass 2+
        # brief-review gaps make the first green report insufficient: the suite
        # passes but the brief is not met, so the fixer runs before green counts
        needs_fix_before_green = bool(brief_gaps)

        while True:
            report = await self._run_suite(run_id)
            if report is None:
                # the suite cannot run even after a toolchain reinstall: an
                # honest "not green", and the run still writes its docs
                await self.status(Role.FIXER, "stuck", "the test suite could not run; not green")
                return False, TestReport()
            await self.send(
                TestRunFinished(
                    passed=report.passed,
                    failed=report.failed,
                    skipped=report.skipped,
                    errors=report.errors,
                    green=report.green,
                )
            )
            if report.green:
                if needs_fix_before_green:
                    needs_fix_before_green = False
                else:
                    return True, report

            failing_now = report.failed + report.errors
            if last_failing is not None:
                stalls = stalls + 1 if failing_now >= last_failing else 0
            last_failing = failing_now
            if passes >= max_passes or stalls >= FIX_STALL_LIMIT:
                reason = (
                    f"fix cap reached ({max_passes})"
                    if passes >= max_passes
                    else f"no progress in {stalls} fix passes"
                )
                await self.status(Role.FIXER, "stuck", f"{reason}; not green")
                return False, report

            escalate = passes + 1 >= max_passes or stalls >= 1
            groups = self._fix_groups(report, graph)
            diagnoses = await asyncio.gather(
                *(
                    self._fix_pass(
                        run_id,
                        graph,
                        files_by_task,
                        owners,
                        group_report,
                        brief_gaps,
                        attempt_history,
                        escalate,
                        agent_id="fixer" if len(groups) == 1 else f"fixer-{index}",
                    )
                    for index, (owners, group_report) in enumerate(groups, start=1)
                ),
                return_exceptions=True,
            )
            errors = [d for d in diagnoses if isinstance(d, BaseException)]
            for error in errors:
                if isinstance(error, asyncio.CancelledError):
                    raise error
            if errors:
                raise errors[0]
            attempt_history.extend(diagnoses)
            passes += 1

    def _fix_groups(
        self, report: TestReport, graph: TaskGraph
    ) -> list[tuple[list[Task], TestReport]]:
        """Split the failures into independent fix jobs, one per workspace root.

        Parallel fixers only when every failing case blames tasks in exactly one
        root and at least two roots are involved. A case that implicates two
        roots is usually a contract mismatch between them (frontend calls a
        route the backend named differently): one fixer must see both sides,
        or two fixers each "fix" toward the other.
        """
        whole = [(blame_tasks(report, graph, self._suite), report)]
        failing = [case for case in report.cases if case.outcome in ("failed", "error")]
        if not self.settings.parallel_fixers or not failing:
            return whole
        by_root: dict[str, tuple[dict[str, Task], list]] = {}
        for case in failing:
            owners = blame_tasks(
                report.model_copy(update={"cases": [case]}), graph, self._suite
            )
            roots = {owner.root for owner in owners}
            if len(roots) != 1:
                return whole
            tasks, cases = by_root.setdefault(roots.pop(), ({}, []))
            tasks.update({owner.id: owner for owner in owners})
            cases.append(case)
        if len(by_root) < 2:
            return whole
        passing = [case for case in report.cases if case.outcome not in ("failed", "error")]
        return [
            (list(tasks.values()), report.model_copy(update={"cases": passing + cases}))
            for tasks, cases in by_root.values()
        ]

    async def _fix_pass(
        self,
        run_id: str,
        graph: TaskGraph,
        files_by_task: dict[str, dict[str, str]],
        owners: list[Task],
        report: TestReport,
        brief_gaps: list[str] | None,
        attempt_history: list[str],
        escalate: bool,
        *,
        agent_id: str,
    ) -> str:
        """One fixer over one group of blamed tasks. Returns its diagnosis."""
        owned: list[str] = []
        for owner in owners:
            owned.extend(owner.owned_paths)
        seen: set[str] = set()
        owned_paths = [p for p in owned if not (p in seen or seen.add(p))]
        primary = owners[0]
        # this round's evidence: what the blamed workers admitted was
        # unfinished, so the fixer patches around the known gaps
        blamed_ids = {owner.id for owner in owners}
        # brief-review gaps lead: they are why this round exists
        known_gaps = list(brief_gaps or [])
        known_gaps += [
            gap
            for result in self.worker_results
            if result.task_id in blamed_ids
            for gap in result.known_gaps
        ]
        await self.send(
            MessageSent(
                from_agent=self._task_agent.get(primary.id, "kiln"),
                to_agent=agent_id,
                summary=f"{primary.title} is failing",
            )
        )
        detail = f"patching {primary.title}"
        if escalate:
            detail += " — escalating to the strongest route"
        await self.status(Role.FIXER, "working", detail)
        label = "Felix Fixer" if agent_id == "fixer" else f"Felix Fixer ({agent_id})"
        fix_scope = self._scope(agent_id, label, Role.FIXER, owned_paths)
        await self.call_tool(
            run_id,
            "claim_scope",
            {"owned_paths": owned_paths, "task": "fix", "status": "working"},
            agent=fix_scope,
        )
        try:
            # widen the primary task's owned_paths for validation across implicated tasks
            wide = primary.model_copy(update={"owned_paths": owned_paths})
            skill_kw = self._skill_kwargs(
                Role.FIXER, tuple(primary.required_skills), root=self._root_of(owners)
            )
            briefing = await self._product_briefing(run_id)
            if self.settings.agentic_devs:

                async def fix_tool(name: str, args: dict) -> str:
                    return await self.call_tool(
                        run_id, name, args, agent=fix_scope, record=False
                    )

                team_notes = self._board.task_notes(blamed_ids) if self._board else ""

                async def fix_escalate(payload) -> str:
                    return await self._escalate(
                        Escalation(
                            kind=payload.kind,
                            summary=payload.summary,
                            details=payload.details,
                            source=agent_id,
                            task_id=primary.id,
                        ),
                        wait=True,
                        reply_lane=None,
                    )

                # an index, not bodies: the fixer must read before it may edit
                patch = await fixer.run_agentic(
                    self.gateway,
                    wide,
                    report,
                    wrap_call_tool_with_skills(fix_tool),
                    current_files=None,
                    file_index=owned_paths,
                    team_notes=team_notes,
                    on_escalate=fix_escalate if self.settings.enable_supervisor else None,
                    contract=graph.contract,
                    history=attempt_history,
                    known_gaps=known_gaps,
                    escalate=escalate,
                    transcript=self.transcript,
                    agent_id=agent_id,
                    spec=self._spec,
                    design=briefing["design"],
                    discovery=briefing["discovery"],
                    plan_memo=self._plan_memo,
                    **skill_kw,
                )
            else:
                current_files = await self.read_workspace(run_id, owned_paths)
                if not current_files:
                    for owner in owners:
                        current_files.update(files_by_task.get(owner.id, {}))
                patch = await asyncio.to_thread(
                    fixer.run,
                    self.gateway,
                    wide,
                    report,
                    current_files,
                    graph.contract,
                    history=attempt_history,
                    known_gaps=known_gaps,
                    escalate=escalate,
                    spec=self._spec,
                    design=briefing["design"],
                    discovery=briefing["discovery"],
                    plan_memo=self._plan_memo,
                    **skill_kw,
                )
            if not self.settings.agentic_devs:
                await self.write_files(run_id, patch.files, agent=fix_scope)
            else:
                self._note_written(file.path for file in patch.files)
            files_by_task.setdefault(primary.id, {}).update(
                {file.path: file.content for file in patch.files}
            )
            await self.status(Role.FIXER, "done", patch.diagnosis[:80])
            await self.usage_tick()
            return patch.diagnosis
        finally:
            await self.call_tool(run_id, "release_scope", {}, agent=fix_scope)

    async def _review(
        self,
        run_id: str,
        idea: str,
        spec,
        graph: TaskGraph,
        report: TestReport,
    ) -> ReviewVerdict:
        """Post-green gate: does the app deliver the brief?

        Runs only after the suite goes green — the suite answers "does it
        work", this answers "is it what the person asked for". The verdict is
        evidence-backed gaps only; an empty list means the review cleared.
        """
        await self.status(Role.REVIEWER, "working", "checking the app against the brief")
        scope = self._scope("reviewer", "Remy Reviewer", Role.REVIEWER)
        discovery = (await self.read_workspace(run_id, [requirements.DISCOVERY_DOC])).get(
            requirements.DISCOVERY_DOC, ""
        )
        design = (await self.read_workspace(run_id, [DESIGN_DOC])).get(DESIGN_DOC, "")
        paths = sorted(
            {path for task in graph.tasks for path in task.owned_paths} | {"README.md"}
        )
        if self.settings.agentic_devs:
            async def review_tool(name: str, args: dict) -> str:
                return await self.call_tool(
                    run_id, name, args, agent=scope, record=False
                )

            # an index, not bodies: the agentic reviewer verifies with read_file
            verdict = await reviewer.run_agentic(
                self.gateway,
                idea,
                spec,
                discovery,
                design,
                report,
                {},
                review_tool,
                transcript=self.transcript,
                agent_id="reviewer",
                paths=paths,
                deferred=self._deferred,
                **self._skill_kwargs(Role.REVIEWER),
            )
        else:
            snapshot = await self.read_workspace(run_id, paths)
            verdict = await asyncio.to_thread(
                reviewer.run,
                self.gateway,
                idea,
                spec,
                discovery,
                design,
                report,
                snapshot,
                deferred=self._deferred,
                **self._skill_kwargs(Role.REVIEWER),
            )
        await self.usage_tick()
        if verdict.gaps:
            await self.status(
                Role.REVIEWER,
                "done",
                f"{len(verdict.gaps)} evidence-backed gaps → reopening the fixer",
            )
        else:
            await self.status(Role.REVIEWER, "done", "review clear — the app delivers the brief")
        return verdict


async def run_pipeline(
    run_id: str,
    idea: str,
    user_notes: list[str],
    gateway: Gateway,
    registry: CompanionRegistry,
    send: FloorSend,
    ceiling: int,
    settings: Settings | None = None,
    transcript: TranscriptLog | None = None,
) -> bool:
    orchestrator = Orchestrator(gateway, registry, send, ceiling, settings, transcript)
    return await orchestrator.run(run_id, idea, user_notes)
