"""The full loop with fake agents and a fake relay: green path and capped fix loop."""

import asyncio

import pytest

from kiln.graph import orchestrator
from kiln.graph.orchestrator import Orchestrator, dependency_layers
from kiln.schemas.patch import FixPatch
from kiln.schemas.report import ReportBundle
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import TaskGraph
from kiln.schemas.work import WorkerResult

SPEC = Spec.model_validate(
    {
        "title": "Tasks",
        "summary": "s",
        "stack": "Flask",
        "user_stories": [{"id": "s1", "as_a": "u", "i_want": "w", "so_that": "t"}],
        "acceptance_criteria": [
            {
                "id": "c1",
                "story_id": "s1",
                "given": "g",
                "when": "w",
                "then": "t",
                "oracle": "tests/test_x.py — check",
            }
        ],
        "file_plan": [{"path": "app.py", "purpose": "app"}],
    }
)

GRAPH = TaskGraph.model_validate(
    {
        "spec_title": "Tasks",
        "tasks": [
            {"id": "t1", "title": "Models", "description": "d", "owned_paths": ["models.py"]},
            {
                "id": "t2",
                "title": "API",
                "description": "d",
                "depends_on": ["t1"],
                "owned_paths": ["app.py"],
            },
        ],
    }
)

SUITE = TestSuite.model_validate(
    {
        "spec_title": "Tasks",
        "files": [
            {"path": "tests/test_x.py", "content": "def test_x():\n    ...", "covers": ["c1"]}
        ],
    },
    context={"criterion_ids": ["c1"]},
)

BUNDLE = ReportBundle(readme="r", run_summary="s", cost_report="c")


class FakeUsage:
    def summary(self):
        return {"calls": 1, "prompt_tokens": 10, "completion_tokens": 10}


class FakeGateway:
    usage = FakeUsage()


class FakeRegistry:
    """Records every relayed tool call; run_tests answers from a script."""

    def __init__(self, test_script):
        self.calls = []
        self.test_script = list(test_script)

    async def call_tool(self, run_id, tool, tool_input, timeout=300):
        self.calls.append((tool, tool_input))
        if tool == "run_tests":
            return self.test_script.pop(0).model_dump_json()
        if tool == "read_file":
            raise Exception("not found")
        return "ok"


class Collector:
    def __init__(self):
        self.events = []

    async def send(self, event):
        self.events.append(event)


def _work(task_id, path):
    return WorkerResult.model_validate(
        {
            "task_id": task_id,
            "summary": f"{task_id} done",
            "files": [{"path": path, "content": "# code\n"}],
            "what_works": "everything",
            "known_gaps": [],
        },
        context={"task_id": task_id, "owned_paths": [path]},
    )


def _patch(path):
    return FixPatch.model_validate(
        {
            "diagnosis": "found it",
            "files": [{"path": path, "content": "# fixed\n"}],
        },
        context={"owned_paths": [path]},
    )


def _agents(monkeypatch, reports):
    monkeypatch.setattr(orchestrator.requirements, "run", lambda *a: SPEC)
    monkeypatch.setattr(orchestrator.planner, "run", lambda *a: GRAPH)
    monkeypatch.setattr(orchestrator.test_author, "run", lambda *a: SUITE)
    monkeypatch.setattr(
        orchestrator.worker, "run", lambda gw, task, *a: _work(task.id, task.owned_paths[0])
    )
    monkeypatch.setattr(orchestrator.fixer, "run", lambda gw, task, *a: _patch(task.owned_paths[0]))
    monkeypatch.setattr(orchestrator.reporter, "run", lambda *a, **k: BUNDLE)


def _report(passed=0, failed=0, errors=0, green=False):
    cases = [TestCaseResult(nodeid=f"t{i}", outcome="passed") for i in range(passed)]
    cases += [TestCaseResult(nodeid=f"f{i}", outcome="failed") for i in range(failed)]
    cases += [TestCaseResult(nodeid=f"e{i}", outcome="error") for i in range(errors)]
    return TestReport(cases=cases)


@pytest.fixture()
def green_setup(monkeypatch):
    _agents(monkeypatch, None)
    registry = FakeRegistry([_report(passed=2, green=True)])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, ceiling=200_000)
    return orch, registry, events


def test_layers_respect_dependencies():
    layers = dependency_layers(GRAPH)
    assert [[task.id for task in layer] for layer in layers] == [["t1"], ["t2"]]


def test_the_full_loop_goes_green(green_setup):
    orch, registry, events = green_setup
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is True

    kinds = [event.kind for event in events.events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert "task_assigned" in kinds
    assert kinds.count("test_run_finished") == 1  # green on the first run
    assert events.events[-1].green is True

    written = [tool_input["path"] for tool, tool_input in registry.calls if tool == "write_file"]
    assert "tests/test_x.py" in written  # suite lands before the workers start
    assert "models.py" in written and "app.py" in written
    assert "README.md" in written  # reporter documents the run


def test_the_fix_loop_repairs_then_passes(monkeypatch):
    _agents(monkeypatch, None)
    registry = FakeRegistry([_report(passed=1, failed=1), _report(passed=2, green=True)])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, ceiling=200_000)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is True
    kinds = [event.kind for event in events.events]
    assert kinds.count("test_run_finished") == 2  # red, then green
    fixer_writes = [
        tool_input["path"]
        for tool, tool_input in registry.calls
        if tool == "write_file" and tool_input["content"] == "# fixed\n"
    ]
    assert fixer_writes  # the patch was applied


def test_the_fix_loop_respects_the_cap_and_finishes_not_green(monkeypatch):
    _agents(monkeypatch, None)
    registry = FakeRegistry([_report(passed=1, failed=1) for _ in range(10)])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, ceiling=200_000)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is False
    kinds = [event.kind for event in events.events]
    assert kinds[-1] == "run_finished"
    assert events.events[-1].green is False
    # the cap stops the bleeding: far fewer than 10 scripted reports are consumed
    assert len(registry.test_script) > 0


def test_an_agent_failure_ends_the_run_honestly(monkeypatch):
    _agents(monkeypatch, None)
    monkeypatch.setattr(
        orchestrator.requirements, "run", lambda *a: (_ for _ in ()).throw(ValueError("boom"))
    )
    registry = FakeRegistry([])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, ceiling=200_000)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is False
    assert events.events[-1].green is False
