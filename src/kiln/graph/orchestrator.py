"""The full loop: idea to tested app, driven as one async orchestration.

LangGraph stays for the stub walk-through; the real loop lives here because
the worker and runner stages are async and stateful — parallel fan-out, the
companion relay, and the bounded fix loop. Every stage emits the typed floor
events so the office shows a real run, and every artifact is validated by the
agents' own contracts.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

from kiln.agents import fixer, planner, reporter, requirements, test_author, worker
from kiln.agents.definitions import definition_for
from kiln.agents.roles import Role
from kiln.companion.registry import CompanionRegistry
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
from kiln.schemas.results import TestReport
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Task, TaskGraph
from kiln.schemas.work import WorkerResult

FloorSend = Callable[[object], Awaitable[None]]

DEMO_AGENTS = {
    Role.REQUIREMENTS: AgentRef(
        id="requirements", role=Role.REQUIREMENTS, label="Riley Requirements"
    ),
    Role.PLANNER: AgentRef(id="planner", role=Role.PLANNER, label="Pat Planner"),
    Role.TEST_AUTHOR: AgentRef(id="test_author", role=Role.TEST_AUTHOR, label="Tessa Tests"),
    Role.FIXER: AgentRef(id="fixer", role=Role.FIXER, label="Felix Fixer"),
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


class Orchestrator:
    def __init__(
        self,
        gateway: Gateway,
        registry: CompanionRegistry,
        send: FloorSend,
        ceiling: int,
    ):
        self.gateway = gateway
        self.registry = registry
        self.send = send
        self.ceiling = ceiling
        self.worker_results: list[WorkerResult] = []

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

    async def call_tool(self, run_id: str, tool: str, tool_input: dict) -> str:
        return await self.registry.call_tool(run_id, tool, tool_input, timeout=300)

    async def write_files(self, run_id: str, files) -> None:
        for file in files:
            await self.call_tool(
                run_id, "write_file", {"path": file.path, "content": file.content}
            )

    async def read_workspace(self, run_id: str, paths: list[str]) -> dict[str, str]:
        contents: dict[str, str] = {}
        for path in paths:
            try:
                contents[path] = await self.call_tool(run_id, "read_file", {"path": path})
            except Exception:
                continue  # a file that never landed is simply absent
        return contents

    async def run_tests(self, run_id: str) -> TestReport:
        output = await self.call_tool(run_id, "run_tests", {})
        return TestReport.model_validate_json(output)

    async def run(self, run_id: str, idea: str, user_notes: list[str]) -> bool:
        started = time.monotonic()
        notes = tuple(user_notes)
        await self.send(RunStarted(run_id=run_id, idea=idea))
        try:
            green = await self._phases(run_id, idea, notes)
        except Exception as exc:
            await self.send(
                AgentStatus(agent=DEMO_AGENTS[Role.REPORTER], status="stuck", detail=str(exc)[:200])
            )
            green = False
        wall = time.monotonic() - started
        await self.send(RunFinished(run_id=run_id, green=green, wall_clock_seconds=round(wall, 1)))
        return green

    async def _phases(self, run_id: str, idea: str, notes: tuple[str, ...]) -> bool:
        # requirements
        await self.status(Role.REQUIREMENTS, "thinking", "reading the idea")
        spec = await asyncio.to_thread(requirements.run, self.gateway, idea, notes)
        await self.status(Role.REQUIREMENTS, "done", f"spec: {spec.title}")
        await self.usage_tick()
        await self.send(
            MessageSent(from_agent="requirements", to_agent="planner", summary="spec.md ready")
        )

        # planner
        await self.status(Role.PLANNER, "thinking", "splitting the spec into tasks")
        graph = await asyncio.to_thread(planner.run, self.gateway, spec, notes)
        await self.status(Role.PLANNER, "done", f"{len(graph.tasks)} tasks planned")
        await self.usage_tick()

        # test author
        await self.status(Role.TEST_AUTHOR, "working", "writing tests from the spec")
        suite = await asyncio.to_thread(test_author.run, self.gateway, spec)
        await self.status(Role.TEST_AUTHOR, "done", f"{len(suite.files)} test files ready")
        await self.usage_tick()
        await self.write_files(run_id, suite.files)

        # workers, layer by layer
        layers = dependency_layers(graph)
        files_by_task: dict[str, dict[str, str]] = {}
        for layer_index, layer in enumerate(layers):
            results = await self._run_layer(run_id, layer, layer_index, spec, suite, files_by_task)
            self.worker_results.extend(results)
            for result in results:
                await self.write_files(run_id, result.files)
                files_by_task[result.task_id] = {
                    file.path: file.content for file in result.files
                }

        # test + fix loop, then report on the final verdict
        green, report = await self._test_and_fix(run_id, graph, files_by_task)

        # reporter
        await self.status(Role.REPORTER, "working", "writing README and cost report")
        bundle = await asyncio.to_thread(
            reporter.run,
            self.gateway,
            spec,
            graph,
            self.worker_results,
            report,
            self.gateway.usage.summary(),
        )
        from kiln.schemas.work import WorkFile

        await self.write_files(
            run_id,
            [
                WorkFile(path="README.md", content=bundle.readme),
                WorkFile(path="docs/run-summary.md", content=bundle.run_summary),
                WorkFile(path="docs/cost-report.md", content=bundle.cost_report),
            ],
        )
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
    ) -> list[WorkerResult]:
        async def one(task: Task, index: int) -> WorkerResult:
            ref = worker_ref(layer_index * 4 + index + 1)
            await self.send(
                TaskAssigned(
                    task=TaskRef(id=task.id, title=task.title, owned_paths=task.owned_paths),
                    agent_id=ref.id,
                )
            )
            await self.send(
                AgentStatus(agent=ref, status="working", detail=f"writing {task.title}")
            )
            # a worker sees exactly what its dependencies produced, nothing more
            current: dict[str, str] = {}
            for dependency in task.depends_on:
                current.update(files_by_task.get(dependency, {}))
            result = await asyncio.to_thread(
                worker.run, self.gateway, task, spec, suite, current
            )
            await self.send(AgentStatus(agent=ref, status="done", detail=result.summary[:80]))
            await self.usage_tick()
            return result

        return await asyncio.gather(*(one(task, i) for i, task in enumerate(layer)))

    async def _test_and_fix(
        self, run_id: str, graph: TaskGraph, files_by_task: dict[str, dict[str, str]]
    ) -> tuple[bool, TestReport]:
        max_attempts = definition_for(Role.FIXER).max_fix_attempts
        tasks_by_id = {task.id: task for task in graph.tasks}
        attempts = 0
        last_failing = None

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
                return True, report
            if attempts >= max_attempts:
                await self.status(
                    Role.FIXER, "stuck", f"fix cap reached ({max_attempts}); escalating"
                )
                return False, report

            # the no-progress probe: a failing count that does not move means
            # the fixer is grinding, so the cap burns faster
            failing_now = report.failed + report.errors
            if last_failing is not None and failing_now >= last_failing:
                attempts += 1
            last_failing = failing_now

            owner = tasks_by_id.get(self._blame(report, graph)) or graph.tasks[0]
            await self.send(
                MessageSent(
                    from_agent="worker-1", to_agent="fixer", summary=f"{owner.title} is failing"
                )
            )
            await self.status(Role.FIXER, "working", f"patching {owner.title}")
            current_files = await self.read_workspace(run_id, owner.owned_paths)
            if not current_files:
                current_files = files_by_task.get(owner.id, {})
            patch = await asyncio.to_thread(fixer.run, self.gateway, owner, report, current_files)
            attempts += 1
            await self.write_files(run_id, patch.files)
            files_by_task.setdefault(owner.id, {}).update(
                {file.path: file.content for file in patch.files}
            )
            await self.status(Role.FIXER, "done", patch.diagnosis[:80])
            await self.usage_tick()

    def _blame(self, report: TestReport, graph: TaskGraph) -> str:
        """Which task most likely owns the failure, from failing node ids."""
        failing = " ".join(case.nodeid for case in report.cases if case.outcome != "passed")
        for task in graph.tasks:
            stem = task.id.lower().replace("-", "_")
            if stem in failing.lower():
                return task.id
        return graph.tasks[0].id


async def run_pipeline(
    run_id: str,
    idea: str,
    user_notes: list[str],
    gateway: Gateway,
    registry: CompanionRegistry,
    send: FloorSend,
    ceiling: int,
) -> bool:
    orchestrator = Orchestrator(gateway, registry, send, ceiling)
    return await orchestrator.run(run_id, idea, user_notes)
