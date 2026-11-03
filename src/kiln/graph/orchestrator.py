"""The full loop: idea to tested app, driven as one async orchestration.

Requirements may ask the user; the stack is probed and installed on the host
with approval; developers run as tool-using agents with path leases so two
devs cannot overwrite each other; tests run on the host by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from kiln.agents import fixer, planner, reporter, requirements, reviewer, test_author, worker
from kiln.agents.definitions import definition_for
from kiln.agents.planner import CONTRACT_DOC, DESIGN_DOC, PLAN_DOC, render_plan_markdown
from kiln.agents.roles import Role
from kiln.agents.skills import (
    clear_project_skills,
    needs_prod_ops_skill,
    needs_security_skill,
    needs_ui_product_skill,
    register_project_skill_texts,
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


def blame_tasks(report: TestReport, graph: TaskGraph, suite: TestSuite | None) -> list[Task]:
    """Map failing tests to tasks via suite covers → criterion → task.covers."""
    failing = [case for case in report.cases if case.outcome in ("failed", "error")]
    if not failing:
        return _fallback_owners(graph)

    criterion_hits: set[str] = set()
    if suite is not None:
        for case in failing:
            for file in suite.files:
                stem = file.path.rsplit("/", 1)[-1]
                if stem in case.nodeid or file.path in case.nodeid:
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
        self._worker_seq = 0

    def _env(self, run_id: str) -> dict:
        run = self.registry.get_run(run_id)
        return dict(run.environment) if run else {}

    async def _product_briefing(self, run_id: str) -> dict[str, str]:
        """Load design + discovery for coding agents when inject_product_briefing is on."""
        if not self.settings.inject_product_briefing:
            return {"design": "", "discovery": ""}
        design = (await self.read_workspace(run_id, [DESIGN_DOC])).get(DESIGN_DOC, "")
        discovery = (await self.read_workspace(run_id, [requirements.DISCOVERY_DOC])).get(
            requirements.DISCOVERY_DOC, ""
        )
        return {"design": design or "", "discovery": discovery or ""}

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
        self, run_id: str, tool: str, tool_input: dict, agent: dict | None = None
    ) -> str:
        aid = (agent or {}).get("id", "orchestrator")
        role = (agent or {}).get("role", "orchestrator")
        self.transcript.record(aid, role, "tool_call", {"name": tool, "args": tool_input})
        try:
            output = await self.registry.call_tool(
                run_id, tool, tool_input, timeout=300, agent=agent
            )
        except Exception as exc:
            self.transcript.record(aid, role, "tool_result", {"name": tool, "error": str(exc)})
            raise
        self.transcript.record(aid, role, "tool_result", {"name": tool, "output": output})
        return output

    async def write_files(self, run_id: str, files, agent: dict | None = None) -> None:
        for file in files:
            await self.call_tool(
                run_id,
                "write_file",
                {"path": file.path, "content": file.content},
                agent=agent,
            )

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

    async def run(self, run_id: str, idea: str, user_notes: list[str]) -> bool:
        started = time.monotonic()
        await self.send(RunStarted(run_id=run_id, idea=idea))
        try:
            green = await self._phases(run_id, idea, tuple(user_notes))
        except Exception as exc:
            logger.exception("run %s failed", run_id)
            await self.send(
                AgentStatus(
                    agent=DEMO_AGENTS[Role.REPORTER], status="stuck", detail=str(exc)[:200]
                )
            )
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

    def _skill_kwargs(self) -> dict:
        return {
            "ui_product": self._ui_product,
            "prod_ops": self._prod_ops,
            "security": self._security,
        }

    async def _run_planner(self, spec, notes: tuple[str, ...], plan_tool) -> TaskGraph:
        tool = wrap_call_tool_with_skills(plan_tool)
        if self.settings.agentic_devs:
            return await planner.run_agentic(
                self.gateway,
                spec,
                notes,
                call_tool=tool,
                **self._skill_kwargs(),
            )
        return await asyncio.to_thread(
            planner.run, self.gateway, spec, notes, **self._skill_kwargs()
        )

    async def _product_gates(self, run_id: str, idea: str) -> list[str]:
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
                raw = await self.call_tool(run_id, "runtime_smoke", {"paths": ["/"]})
                payload = json.loads(raw)
                gaps.extend(payload.get("gaps") or [])
            except ToolDenied:
                pass
            except Exception as exc:  # noqa: BLE001
                # Spawn/PATH failures used to surface as FileNotFoundError and
                # still reach the fixer via browser_verify; keep them advisory.
                logger.warning("runtime_smoke failed (host env, not forcing fix): %s", exc)
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
        actionable, host_env = partition_product_gaps(gaps)
        for gap in host_env:
            logger.warning("product gate skipped (host env): %s", gap)
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

        lines = [f"{len(graph.tasks)} tasks:"]
        for task in graph.tasks:
            lines.append(f"{task.id} [{task.kind}] owns {', '.join(task.owned_paths)}")
        try:
            await self.call_tool(
                run_id, "confirm_plan", {"lines": lines, "round": 1}, agent=scope
            )
            await self.status(Role.PLANNER, "done", "plan approved")
            return graph
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
            lines2 = [f"revised plan — {len(graph2.tasks)} tasks:"]
            for task in graph2.tasks:
                lines2.append(f"{task.id} [{task.kind}] owns {', '.join(task.owned_paths)}")
            try:
                await self.call_tool(
                    run_id, "confirm_plan", {"lines": lines2, "round": 2}, agent=scope
                )
            except ToolDenied as second:
                raise LoopError(f"plan not approved: {second}") from second
            await self.status(Role.PLANNER, "done", "revised plan approved")
            await self.usage_tick()
            return graph2

    async def _phases(self, run_id: str, idea: str, notes: tuple[str, ...]) -> bool:
        self._worker_seq = 0
        req_scope = self._scope("requirements", "Riley Requirements", Role.REQUIREMENTS)

        async def req_tool(name: str, args: dict) -> str:
            return await self.call_tool(run_id, name, args, agent=req_scope)

        await self._load_project_skills(run_id)
        await self.status(Role.REQUIREMENTS, "thinking", "reading the idea")
        spec = await requirements.run_async(
            self.gateway,
            idea,
            self._notes(run_id, notes),
            call_tool=req_tool,
            ask=self.settings.ask_requirements,
        )
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
        await self.status(Role.REQUIREMENTS, "done", f"spec: {spec.title}")
        await self.usage_tick()
        await self.send(
            MessageSent(from_agent="requirements", to_agent="planner", summary="spec.md ready")
        )

        await self._resolve_stack(run_id, spec.stack_id)
        profile = profile_for(self.stack_id)

        graph = await self._plan(run_id, spec, notes)

        await self.status(Role.TEST_AUTHOR, "working", "writing tests from the spec")
        suite = await asyncio.to_thread(
            test_author.run, self.gateway, spec, **self._skill_kwargs()
        )
        self._suite = suite
        self._spec = spec
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

        layers = dependency_layers(graph)
        files_by_task: dict[str, dict[str, str]] = {}
        # Regression context carried between layers. The suite is written
        # against the whole spec, so a red run mid-build is expected; the green
        # list is what the next layer must not break.
        known_green: list[str] = []
        known_broken = 0
        regressions: list[str] = []
        for layer_index, layer in enumerate(layers):
            results = await self._run_layer(
                run_id,
                layer,
                layer_index,
                spec,
                suite,
                files_by_task,
                graph,
                profile.dev_notes,
                test_state=worker.test_state_block(known_green, known_broken, regressions),
            )
            self.worker_results.extend(results)
            for result in results:
                if not self.settings.agentic_devs:
                    await self.write_files(run_id, result.files)
                files_by_task[result.task_id] = {
                    file.path: file.content for file in result.files
                }
            # host dependencies land right after the scaffold layer, so the
            # smoke check and the per-layer suite below run against an
            # installed workspace (docker vendors once, at the end)
            if layer_index == 0 and self.settings.sandbox != "docker":
                await self.call_tool(run_id, "install_deps", {"stack_id": self.stack_id})
            # contract smoke check after each layer
            paths = [p for result in results for p in files_by_task.get(result.task_id, {})]
            if paths:
                try:
                    await self.call_tool(
                        run_id,
                        "smoke_check",
                        {"stack_id": self.stack_id, "paths": paths},
                    )
                except ToolDenied:
                    pass  # the user declined; the real suite still decides green
                except Exception as exc:
                    # a smoke command ran and failed (syntax/import): the layer
                    # is broken — stop instead of building on top of it
                    raise LoopError(
                        f"smoke check failed after layer {layer_index + 1}: {exc}"
                    ) from exc
            # CI after every increment: the suite runs after each layer but the
            # last — the fix loop's first run covers the final layer
            if layer_index < len(layers) - 1:
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
                green_now = [
                    case.nodeid for case in report.cases if case.outcome == "passed"
                ]
                broken_now = {
                    case.nodeid
                    for case in report.cases
                    if case.outcome in ("failed", "error")
                }
                regressions = [nodeid for nodeid in known_green if nodeid in broken_now]
                known_green = green_now
                known_broken = len(broken_now)

        if self.settings.sandbox == "docker":
            await self.call_tool(run_id, "vendor_deps", {})

        green, report = await self._test_and_fix(run_id, graph, files_by_task)

        review: ReviewVerdict | None = None
        if green:
            product_gaps = await self._product_gates(run_id, idea)
            if product_gaps:
                green, report = await self._test_and_fix(
                    run_id,
                    graph,
                    files_by_task,
                    brief_gaps=product_gaps,
                )
            if green:
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

        await self.status(Role.REPORTER, "working", "writing README and cost report")
        reporter_scope = self._scope(
            "reporter",
            "Rosa Reporter",
            Role.REPORTER,
            ["README.md", "AGENTS.md", "docs/"],
        )
        if self.settings.agentic_devs:
            # agentic reporter: read-only verification (glob/grep/read) before
            # it commits claims to the documents
            async def report_tool(name: str, args: dict) -> str:
                return await self.call_tool(run_id, name, args, agent=reporter_scope)

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
        report_files = [
            WorkFile(path="README.md", content=bundle.readme),
            WorkFile(path="docs/run-summary.md", content=bundle.run_summary),
            WorkFile(path="docs/cost-report.md", content=bundle.cost_report),
        ]
        if (bundle.agents_md or "").strip():
            report_files.append(WorkFile(path="AGENTS.md", content=bundle.agents_md))
        await self.write_files(run_id, report_files, agent=reporter_scope)
        await self.call_tool(run_id, "release_scope", {}, agent=reporter_scope)
        await self.status(Role.REPORTER, "done", "docs ready")
        await self.usage_tick()
        return green

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
        semaphore = asyncio.Semaphore(self.settings.max_parallel_devs)
        # Dense monotonic IDs (worker-1,2,3…) so the floor can seat each desk
        # without colliding with layer*4 gaps that reused worker-1/2 seats.
        refs = []
        for _ in layer:
            self._worker_seq += 1
            refs.append(worker_ref(self._worker_seq))

        async def one(task: Task, index: int) -> WorkerResult:
            async with semaphore:
                ref = refs[index]
                await self.send(
                    TaskAssigned(
                        task=TaskRef(
                            id=task.id, title=task.title, owned_paths=task.owned_paths
                        ),
                        agent_id=ref.id,
                    )
                )
                await self.send(
                    AgentStatus(agent=ref, status="working", detail=f"writing {task.title}")
                )
                scope = self._scope(ref.id, ref.label, Role.WORKER, task.owned_paths)
                await self.call_tool(
                    run_id,
                    "claim_scope",
                    {
                        "owned_paths": task.owned_paths,
                        "task": task.title,
                        "status": "working",
                    },
                    agent=scope,
                )
                current: dict[str, str] = {}
                for dependency in task.depends_on:
                    current.update(files_by_task.get(dependency, {}))

                async def call_tool(name: str, args: dict) -> str:
                    if name == "list_peers":
                        return await self.call_tool(run_id, name, args, agent=scope)
                    return await self.call_tool(run_id, name, args, agent=scope)

                briefing = await self._product_briefing(run_id)
                try:
                    if self.settings.agentic_devs:
                        # agentic mode passes references, not blobs: the files
                        # are on the user's disk and the worker has read_file
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
                            **self._skill_kwargs(),
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
                            **self._skill_kwargs(),
                        )
                finally:
                    await self.call_tool(run_id, "release_scope", {}, agent=scope)

                await self.send(
                    AgentStatus(agent=ref, status="done", detail=result.summary[:80])
                )
                await self.usage_tick()
                return result

        return await asyncio.gather(*(one(task, i) for i, task in enumerate(layer)))

    async def _test_and_fix(
        self,
        run_id: str,
        graph: TaskGraph,
        files_by_task: dict[str, dict[str, str]],
        brief_gaps: list[str] | None = None,
    ) -> tuple[bool, TestReport]:
        max_attempts = definition_for(Role.FIXER).max_fix_attempts
        attempts = 0
        last_failing = None
        attempt_history: list[str] = []  # what each patch diagnosed, for attempt 2+
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
            if attempts >= max_attempts:
                await self.status(
                    Role.FIXER, "stuck", f"fix cap reached ({max_attempts}); not green"
                )
                return False, report

            failing_now = report.failed + report.errors
            if last_failing is not None and failing_now >= last_failing:
                attempts += 1
            last_failing = failing_now

            owners = blame_tasks(report, graph, self._suite)
            owned = []
            for owner in owners:
                owned.extend(owner.owned_paths)
            # unique while preserving order
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
            # the final allowed attempt runs on the strongest configured route:
            # if it fails the run ends anyway, so do not burn it on the same model
            escalate = attempts + 1 >= max_attempts
            await self.send(
                MessageSent(
                    from_agent="worker-1",
                    to_agent="fixer",
                    summary=f"{primary.title} is failing",
                )
            )
            detail = f"patching {primary.title}"
            if escalate:
                detail += " — last attempt, escalating"
            await self.status(Role.FIXER, "working", detail)
            fix_scope = self._scope("fixer", "Felix Fixer", Role.FIXER, owned_paths)
            await self.call_tool(
                run_id,
                "claim_scope",
                {"owned_paths": owned_paths, "task": "fix", "status": "working"},
                agent=fix_scope,
            )
            try:
                current_files = await self.read_workspace(run_id, owned_paths)
                if not current_files:
                    for owner in owners:
                        current_files.update(files_by_task.get(owner.id, {}))
                # widen the primary task's owned_paths for validation across implicated tasks
                wide = primary.model_copy(update={"owned_paths": owned_paths})
                skill_kw = {
                    "ui_product": self._ui_product,
                    "prod_ops": self._prod_ops,
                    "security": self._security,
                }
                briefing = await self._product_briefing(run_id)
                if self.settings.agentic_devs:

                    async def fix_tool(name: str, args: dict) -> str:
                        return await self.call_tool(
                            run_id, name, args, agent=fix_scope
                        )

                    patch = await fixer.run_agentic(
                        self.gateway,
                        wide,
                        report,
                        wrap_call_tool_with_skills(fix_tool),
                        current_files=current_files,
                        contract=graph.contract,
                        history=attempt_history,
                        known_gaps=known_gaps,
                        escalate=escalate,
                        transcript=self.transcript,
                        agent_id="fixer",
                        spec=self._spec,
                        design=briefing["design"],
                        discovery=briefing["discovery"],
                        **skill_kw,
                    )
                else:
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
                        **skill_kw,
                    )
                attempts += 1
                attempt_history.append(patch.diagnosis)
                if not self.settings.agentic_devs:
                    await self.write_files(run_id, patch.files, agent=fix_scope)
                files_by_task.setdefault(primary.id, {}).update(
                    {file.path: file.content for file in patch.files}
                )
                await self.status(Role.FIXER, "done", patch.diagnosis[:80])
                await self.usage_tick()
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
        snapshot = await self.read_workspace(run_id, paths)
        if self.settings.agentic_devs:
            async def review_tool(name: str, args: dict) -> str:
                return await self.call_tool(run_id, name, args, agent=scope)

            verdict = await reviewer.run_agentic(
                self.gateway,
                idea,
                spec,
                discovery,
                design,
                report,
                snapshot,
                review_tool,
                transcript=self.transcript,
                agent_id="reviewer",
                **self._skill_kwargs(),
            )
        else:
            verdict = await asyncio.to_thread(
                reviewer.run,
                self.gateway,
                idea,
                spec,
                discovery,
                design,
                report,
                snapshot,
                **self._skill_kwargs(),
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
