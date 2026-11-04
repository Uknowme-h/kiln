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

from kiln.agents import fixer, planner, reporter, requirements, reviewer, test_author, worker
from kiln.agents.definitions import definition_for
from kiln.agents.planner import CONTRACT_DOC, DESIGN_DOC, PLAN_DOC, render_plan_markdown
from kiln.agents.roles import Role
from kiln.agents.skills import (
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
from kiln.companion.registry import CompanionRegistry, ToolDenied
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
from kiln.product.browser_flows import fallback_flow_plan
from kiln.schemas.browser_flow import BROWSER_FLOWS_DOC
from kiln.schemas.results import TestReport
from kiln.schemas.review import ReviewVerdict
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Task, TaskGraph
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

DEMO_AGENTS = {
    Role.REQUIREMENTS: AgentRef(
        id="requirements", role=Role.REQUIREMENTS, label="Riley Requirements"
    ),
    Role.PLANNER: AgentRef(id="planner", role=Role.PLANNER, label="Pat Planner"),
    Role.TEST_AUTHOR: AgentRef(id="test_author", role=Role.TEST_AUTHOR, label="Tessa Tests"),
    Role.FIXER: AgentRef(id="fixer", role=Role.FIXER, label="Felix Fixer"),
    Role.REVIEWER: AgentRef(id="reviewer", role=Role.REVIEWER, label="Remy Reviewer"),
    Role.REPORTER: AgentRef(id="reporter", role=Role.REPORTER, label="Rosa Reporter"),
}


def worker_ref(index: int) -> AgentRef:
    return AgentRef(id=f"worker-{index}", role=Role.WORKER, label=f"Worker {index}")


class LoopError(RuntimeError):
    """The loop cannot continue; the run ends honestly, not green."""


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _first_lines(text: str, lines: int = 4, limit: int = 400) -> str:
    """The head of a tool error, ANSI colours stripped, for a one-line reason."""
    clean = _ANSI.sub("", text).strip()
    head = " | ".join(line.strip() for line in clean.splitlines()[:lines] if line.strip())
    return head[:limit]


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


class Orchestrator:
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
            output = await self.registry.call_tool(
                run_id, tool, tool_input, timeout=300, agent=agent
            )
        except Exception as exc:
            if record:
                self.transcript.record(aid, role, "tool_result", {"name": tool, "error": str(exc)})
            raise
        if record:
            self.transcript.record(aid, role, "tool_result", {"name": tool, "output": output})
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
        wall = time.monotonic() - started
        await self.send(RunFinished(run_id=run_id, green=green, wall_clock_seconds=round(wall, 1)))
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

        A scaffold that fails or is declined ends the run. Every later agent is
        told the root is already scaffolded, so building on a missing skeleton
        only spends tokens on files no toolchain can run (run-9bbe61f0 wrote a
        Next.js app with no package.json, then died at the first test run).
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
                raise LoopError(
                    f"the {framework} scaffold at {root.path!r} was declined, and every "
                    f"task builds on it; approve the scaffold to build this plan ({exc})"
                ) from exc
            except Exception as exc:  # noqa: BLE001 — classified below
                if "already scaffolded" in str(exc):
                    # an existing project: the root is reused, not a failure
                    self._scaffolded[root.path] = framework
                    logger.info("%s at %s already scaffolded; reusing it", framework, root.path)
                    continue
                logger.warning("scaffold_framework(%s @ %s) failed: %s", framework, root.path, exc)
                raise LoopError(
                    f"scaffolding {framework} at {root.path!r} failed, and every task "
                    f"builds on it: {_first_lines(str(exc))}"
                ) from exc

    async def _install_all_roots(self, run_id: str, spec) -> None:
        """Install dependencies for every declared root after the scaffold layer.

        Each root installs with its own toolchain at its own path: a node root
        (vite_react/nextjs/express/nestjs) runs npm there, a python root builds
        its venv there. The primary stack (stack_id) still owns the real test
        suite; the other root only needs deps present so the secondary smoke
        check can compile/type-check it.
        """
        from kiln.schemas.spec import UI_FRAMEWORKS

        def stack_for(framework: str) -> str:
            return "node" if framework in UI_FRAMEWORKS or framework in ("express", "nestjs") else "python"

        roots = [r for r in (spec.roots or []) if r.framework]
        if not roots:
            await self.call_tool(run_id, "install_deps", {"stack_id": self.stack_id})
            return
        scope = self._scope("orchestrator", "Kiln", Role.PLANNER)
        suite_root_done = False
        for root in roots:
            root_stack = stack_for(root.framework)
            is_suite_root = root.path in ("", ".") and root_stack == self.stack_id
            try:
                await self.call_tool(
                    run_id,
                    "install_deps",
                    {"stack_id": root_stack, "root": root.path},
                    agent=scope,
                )
            except Exception as exc:  # noqa: BLE001 — a secondary root's install is advisory
                if is_suite_root:
                    raise LoopError(
                        f"installing the {root_stack} dependencies the suite needs failed: "
                        f"{_first_lines(str(exc))}"
                    ) from exc
                logger.warning("install_deps(%s @ %s) failed: %s", root_stack, root.path, exc)
            suite_root_done = suite_root_done or is_suite_root
        # The suite runs at the workspace root. A node suite over a split
        # product (frontend/ + backend/) has no declared root there, so nothing
        # would ever install its runner; install it explicitly.
        if self.stack_id == "node" and not suite_root_done:
            try:
                await self.call_tool(
                    run_id, "install_deps", {"stack_id": "node", "root": "."}, agent=scope
                )
            except Exception as exc:  # noqa: BLE001
                raise LoopError(
                    f"installing the test runner at the workspace root failed: "
                    f"{_first_lines(str(exc))}"
                ) from exc

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

    @staticmethod
    def _plan_lines(spec, graph: TaskGraph, lead: str) -> list[str]:
        """What the user approves: the tasks, and — never hidden — what is deferred."""
        lines = [lead]
        for task in graph.tasks:
            lines.append(f"{task.id} [{task.kind}] owns {', '.join(task.owned_paths)}")
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
                if "{" in path or "}" in path or path.startswith("/api"):
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

    async def _build(
        self,
        run_id: str,
        spec,
        suite: TestSuite,
        graph: TaskGraph,
        stack_notes: str,
    ) -> dict[str, dict[str, str]]:
        """Build every task: a scaffold barrier, then dependency-driven scheduling.

        Stage A runs the tasks with no dependencies (the scaffolds) together,
        then installs dependencies, smoke-checks and runs the suite once — the
        workspace every later task builds on is installed and known-good.

        Stage B starts each remaining task as soon as *its own* dependencies are
        done, up to ``max_parallel_devs``. The old fixed layers made a task wait
        for every task of the previous depth, however unrelated. Each finished
        task is smoke-checked; the suite refreshes at quiescent points (nothing
        running, work still pending) so ``<test_state>`` stays meaningful.
        """
        files_by_task: dict[str, dict[str, str]] = {}
        layers = dependency_layers(graph)
        first = layers[0]
        rest = [task for layer in layers[1:] for task in layer]

        self._at("the scaffold tasks")
        results = await self._run_layer(
            run_id, first, 0, spec, suite, files_by_task, graph, stack_notes
        )
        for result in results:
            await self._land(run_id, result, files_by_task)
        # host dependencies land right after the scaffold stage, so the smoke
        # check and the suite run against an installed workspace (docker vendors
        # once, at the end). Multi-root: each declared root installs at its path.
        if self.settings.sandbox != "docker":
            self._at("installing dependencies")
            await self._install_all_roots(run_id, spec)
        self._at("the smoke check after the scaffold tasks")
        await self._smoke(
            run_id,
            [p for result in results for p in files_by_task.get(result.task_id, {})],
            "the scaffold stage",
        )
        if not rest:
            return files_by_task
        state = _SuiteState()
        self._at("the test run after the scaffold tasks")
        await self._refresh_suite_state(run_id, state)
        self._at("the feature tasks")
        await self._run_dag(
            run_id, rest, spec, suite, files_by_task, graph, stack_notes, state
        )
        return files_by_task

    async def _land(
        self, run_id: str, result: WorkerResult, files_by_task: dict[str, dict[str, str]]
    ) -> None:
        """Record one finished task (and, in one-shot mode, write its files)."""
        self.worker_results.append(result)
        self._results_by_task[result.task_id] = result
        if not self.settings.agentic_devs:
            await self.write_files(run_id, result.files)
        else:
            self._note_written(file.path for file in result.files)
        files_by_task[result.task_id] = {file.path: file.content for file in result.files}

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
            # a smoke command ran and failed (syntax/import): stop instead of
            # building on top of a broken workspace
            raise LoopError(f"smoke check failed after {where}: {exc}") from exc

    async def _refresh_suite_state(self, run_id: str, state: "_SuiteState") -> None:
        """Run the suite and fold the result into the regression state.

        The suite covers the whole in-scope spec, so red mid-build is expected;
        the green list is what later tasks must not break.
        """
        report = await self.run_tests(run_id)
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

    async def _run_dag(
        self,
        run_id: str,
        tasks: list[Task],
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
        state: "_SuiteState",
    ) -> None:
        pending = {task.id: task for task in tasks}
        done = set(files_by_task)
        running: dict[asyncio.Task, Task] = {}
        failure: tuple[Task, BaseException] | None = None
        landed_since_suite = 0

        while pending or running:
            if failure is None and pending:
                ready = [t for t in pending.values() if set(t.depends_on) <= done]
                if not running and ready and landed_since_suite:
                    await self._refresh_suite_state(run_id, state)
                    landed_since_suite = 0
                for task in ready:
                    if len(running) >= self.settings.max_parallel_devs:
                        break
                    del pending[task.id]
                    job = asyncio.create_task(
                        self._run_task(
                            run_id,
                            task,
                            spec,
                            suite,
                            files_by_task,
                            graph,
                            stack_notes,
                            state.block(),
                        )
                    )
                    running[job] = task
            if not running:
                if failure is None and pending:
                    raise LoopError("task dependencies cannot be scheduled")
                break
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for job in finished:
                task = running.pop(job)
                try:
                    result = job.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — recorded, then drained
                    failure = failure or (task, exc)
                    continue
                await self._land(run_id, result, files_by_task)
                done.add(task.id)
                landed_since_suite += 1
                if failure is None:
                    try:
                        await self._smoke(
                            run_id, list(files_by_task.get(task.id, {})), f"task {task.id}"
                        )
                    except LoopError as exc:
                        failure = (task, exc)
            # once something failed, nothing new starts; running workers are
            # drained so none is left holding a lease while the run tears down

        if failure is not None:
            task, exc = failure
            if isinstance(exc, LoopError):
                raise exc
            raise LoopError(f"{task.title} ({task.id}) failed: {exc}") from exc

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
    ) -> WorkerResult:
        """One task end to end, with a fresh worker per attempt.

        A worker that wrote in-scope files returns a salvaged partial result
        and never lands here as a failure; a failure means nothing was
        delivered, so a fresh attempt (told why) is cheap insurance against one
        bad trajectory ending the whole run.
        """
        attempts = 1 + self.settings.worker_retries
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            # Dense monotonic IDs (worker-1,2,3…) so the floor can seat each desk
            self._worker_seq += 1
            ref = worker_ref(self._worker_seq)
            retry_note = str(last)[:300] if last is not None else ""
            try:
                return await self._attempt_task(
                    run_id,
                    task,
                    ref,
                    spec,
                    suite,
                    files_by_task,
                    graph,
                    stack_notes,
                    test_state,
                    retry_note,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced
                last = exc
                await self.send(AgentStatus(agent=ref, status="stuck", detail=str(exc)[:200]))
                if attempt < attempts:
                    logger.warning("%s (%s) failed; retrying with a fresh worker: %s", ref.id, task.id, exc)
        assert last is not None
        suffix = f" after {attempts} attempts" if attempts > 1 else ""
        raise LoopError(f"{ref.id} ({task.title}) failed{suffix}: {last}") from last

    async def _attempt_task(
        self,
        run_id: str,
        task: Task,
        ref: AgentRef,
        spec,
        suite: TestSuite,
        files_by_task: dict[str, dict[str, str]],
        graph: TaskGraph,
        stack_notes: str,
        test_state: str,
        retry_note: str,
    ) -> WorkerResult:
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
        current: dict[str, str] = {}
        for dependency in task.depends_on:
            current.update(files_by_task.get(dependency, {}))
        handoffs = [
            {
                "task_id": dep,
                "summary": self._results_by_task[dep].summary,
                "what_works": self._results_by_task[dep].what_works,
                "known_gaps": list(self._results_by_task[dep].known_gaps),
            }
            for dep in task.depends_on
            if dep in self._results_by_task
        ]

        async def call_tool(name: str, args: dict) -> str:
            # run_agent_loop records these calls; do not double them
            return await self.call_tool(run_id, name, args, agent=scope, record=False)

        briefing = await self._product_briefing(run_id)
        skills = self._skill_kwargs(Role.WORKER, tuple(task.required_skills), root=task.root)
        try:
            if self.settings.agentic_devs:
                # agentic mode passes references, not blobs: the files are on
                # the user's disk and the worker has read_file
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
                    **skills,
                )
        finally:
            await self.call_tool(run_id, "release_scope", {}, agent=scope)

        await self.send(AgentStatus(agent=ref, status="done", detail=result.summary[:80]))
        await self.usage_tick()
        return result

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
        """Run independent tasks together (the scaffold stage).

        return_exceptions: a failing worker must not leave its siblings running
        detached (holding leases, calling tools) while the run tears down. Let
        every worker finish, then fail honestly, naming the task that failed.
        """
        semaphore = asyncio.Semaphore(self.settings.max_parallel_devs)

        async def one(task: Task) -> WorkerResult:
            async with semaphore:
                return await self._run_task(
                    run_id, task, spec, suite, files_by_task, graph, stack_notes, test_state
                )

        outcomes = await asyncio.gather(*(one(task) for task in layer), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        return list(outcomes)

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
            report = await self.run_tests(run_id)
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

                # an index, not bodies: the fixer must read before it may edit
                patch = await fixer.run_agentic(
                    self.gateway,
                    wide,
                    report,
                    wrap_call_tool_with_skills(fix_tool),
                    current_files=None,
                    file_index=owned_paths,
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
