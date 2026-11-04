"""The full loop with fake agents and a fake relay: green path and capped fix loop."""

import asyncio
import json

import pytest

from kiln.companion.registry import ToolDenied
from kiln.config import Settings
from kiln.graph import orchestrator
from kiln.graph.orchestrator import Orchestrator, dependency_layers
from kiln.schemas.patch import FixPatch
from kiln.schemas.report import ReportBundle
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.review import ReviewGap, ReviewVerdict
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Task, TaskGraph
from kiln.schemas.work import WorkerResult

SPEC = Spec.model_validate(
    {
        "title": "Tasks",
        "summary": "s",
        "stack": "Flask",
        "stack_id": "python",
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

CLEAR_REVIEW = ReviewVerdict(gaps=[], summary="the app delivers the brief")

TEST_SETTINGS = Settings(
    _env_file=None,
    agentic_devs=False,
    ask_requirements=False,
    sandbox="host",
    max_parallel_devs=2,
)


class FakeUsage:
    def summary(self):
        return {"calls": 1, "prompt_tokens": 10, "completion_tokens": 10}


class FakeGateway:
    usage = FakeUsage()


class FakeRun:
    def __init__(self, user_notes):
        self.user_notes = list(user_notes)
        self.environment = {"os": "Linux", "shell": {"label": "bash"}}


class FakeRegistry:
    """Records every relayed tool call; run_tests answers from a script."""

    def __init__(self, test_script, user_notes=(), files=None, inventory=()):
        self.calls = []
        self.test_script = list(test_script)
        self.run = FakeRun(user_notes)
        self.files = dict(files or {})
        self.inventory = list(inventory)

    def get_run(self, run_id):
        return self.run

    async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
        self.calls.append((tool, tool_input))
        if tool == "run_tests":
            return self.test_script.pop(0).model_dump_json()
        if tool == "probe_stack":
            return json.dumps({"stack_id": "python", "ready": True, "tools": {}, "label": "Python"})
        if tool in ("served_ui_audit", "runtime_smoke", "browser_verify"):
            return json.dumps({"ok": True, "gaps": []})
        if tool == "list_dir":
            # a fresh workspace unless a test opts into an existing project
            return "\n".join(self.inventory) or "(empty)"
        if tool == "read_file":
            if tool_input.get("path") not in self.files:
                raise Exception("not found")
            return self.files[tool_input["path"]]
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


def _agents(monkeypatch):
    async def fake_requirements(*_a, **_k):
        return SPEC

    monkeypatch.setattr(orchestrator.requirements, "run_async", fake_requirements)
    monkeypatch.setattr(orchestrator.planner, "run", lambda *a, **k: GRAPH)
    monkeypatch.setattr(orchestrator.test_author, "run", lambda *a, **k: SUITE)
    monkeypatch.setattr(
        orchestrator.worker,
        "run",
        lambda gw, task, *a, **k: _work(task.id, task.owned_paths[0]),
    )
    monkeypatch.setattr(
        orchestrator.fixer,
        "run",
        lambda gw, task, *a, **k: _patch(task.owned_paths[0]),
    )
    monkeypatch.setattr(orchestrator.reviewer, "run", lambda *a, **k: CLEAR_REVIEW)
    monkeypatch.setattr(orchestrator.reporter, "run", lambda *a, **k: BUNDLE)


def _report(passed=0, failed=0, errors=0, green=False):
    cases = [TestCaseResult(nodeid=f"t{i}", outcome="passed") for i in range(passed)]
    cases += [TestCaseResult(nodeid=f"f{i}", outcome="failed") for i in range(failed)]
    cases += [TestCaseResult(nodeid=f"e{i}", outcome="error") for i in range(errors)]
    return TestReport(cases=cases)


def _orch(registry, events):
    return Orchestrator(
        FakeGateway(), registry, events.send, ceiling=200_000, settings=TEST_SETTINGS
    )


@pytest.fixture()
def green_setup(monkeypatch):
    _agents(monkeypatch)
    # first report = the per-layer suite run after the scaffold, second = green
    registry = FakeRegistry([_report(passed=1, failed=1), _report(passed=2, green=True)])
    events = Collector()
    return _orch(registry, events), registry, events


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
    # one suite run after the scaffold layer, one for the fix loop
    assert kinds.count("test_run_finished") == 2
    assert events.events[-1].green is True

    written = [tool_input["path"] for tool, tool_input in registry.calls if tool == "write_file"]
    assert "tests/test_x.py" in written
    assert "models.py" in written and "app.py" in written
    assert "README.md" in written


def test_the_fix_loop_repairs_then_passes(monkeypatch):
    _agents(monkeypatch)
    registry = FakeRegistry(
        [
            _report(passed=1, failed=1),  # per-layer run after the scaffold
            _report(passed=1, failed=1),  # the fix loop's first run
            _report(passed=2, green=True),
        ]
    )
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is True
    kinds = [event.kind for event in events.events]
    assert kinds.count("test_run_finished") == 3
    fixer_writes = [
        tool_input["path"]
        for tool, tool_input in registry.calls
        if tool == "write_file" and tool_input["content"] == "# fixed\n"
    ]
    assert fixer_writes


def test_the_fix_loop_respects_the_cap_and_finishes_not_green(monkeypatch):
    _agents(monkeypatch)
    registry = FakeRegistry([_report(passed=1, failed=1) for _ in range(10)])
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is False
    kinds = [event.kind for event in events.events]
    assert kinds[-1] == "run_finished"
    assert events.events[-1].green is False
    assert len(registry.test_script) > 0


def test_notes_arriving_mid_run_reach_the_planner(monkeypatch):
    seen = {}

    async def fake_requirements(gw, idea, notes, **kwargs):
        registry.run.user_notes.append("add due dates")
        return SPEC

    monkeypatch.setattr(orchestrator.requirements, "run_async", fake_requirements)

    def fake_planner(gw, spec, notes, **_k):
        seen["notes"] = notes
        return GRAPH

    monkeypatch.setattr(orchestrator.planner, "run", fake_planner)
    monkeypatch.setattr(orchestrator.test_author, "run", lambda *a, **k: SUITE)
    monkeypatch.setattr(
        orchestrator.worker,
        "run",
        lambda gw, task, *a, **k: _work(task.id, task.owned_paths[0]),
    )
    monkeypatch.setattr(
        orchestrator.fixer,
        "run",
        lambda gw, task, *a, **k: _patch(task.owned_paths[0]),
    )
    monkeypatch.setattr(orchestrator.reviewer, "run", lambda *a, **k: CLEAR_REVIEW)
    monkeypatch.setattr(orchestrator.reporter, "run", lambda *a, **k: BUNDLE)

    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)], user_notes=()
    )
    events = Collector()
    orch = _orch(registry, events)

    asyncio.run(orch.run("run-1", "a to-do app", []))
    assert seen["notes"] == ("add due dates",)


def test_an_agent_failure_ends_the_run_honestly(monkeypatch):
    async def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(orchestrator.requirements, "run_async", boom)
    registry = FakeRegistry([])
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is False
    assert events.events[-1].green is False


# ── the plan-approval beat ──


class ApprovingRegistry(FakeRegistry):
    """Denies confirm_plan from a script of feedback strings ("" = approve)."""

    def __init__(self, test_script, plan_script=(), user_notes=()):
        super().__init__(test_script, user_notes)
        self.plan_script = list(plan_script)

    async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
        if tool == "confirm_plan":
            self.calls.append((tool, tool_input))  # base class never sees this one
            feedback = self.plan_script.pop(0) if self.plan_script else ""
            if feedback:
                raise ToolDenied(f"the user refused this plan: {feedback}")
            return "plan approved"
        return await super().call_tool(run_id, tool, tool_input, timeout, agent)


def test_the_plan_waits_for_approval_before_any_build(green_setup):
    orch, registry, events = green_setup
    asyncio.run(orch.run("run-1", "a to-do app", []))
    order = [tool for tool, _ in registry.calls]
    assert "confirm_plan" in order
    # the approval precedes every worker write (suite/plan docs come earlier)
    worker_writes = [
        i for i, (tool, inp) in enumerate(registry.calls)
        if tool == "write_file" and inp["path"] in ("models.py", "app.py")
    ]
    assert worker_writes and all(i > order.index("confirm_plan") for i in worker_writes)
    assert "docs/contract.md" in [
        inp["path"] for tool, inp in registry.calls if tool == "write_file"
    ]


def test_a_refusal_with_feedback_triggers_one_replan_then_approval(monkeypatch):
    _agents(monkeypatch)
    registry = ApprovingRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)],
        plan_script=["add due dates", ""],
    )
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is True
    confirms = [c for c in registry.calls if c[0] == "confirm_plan"]
    assert len(confirms) == 2  # refused once, approved on the second


def test_a_second_refusal_ends_the_run_not_green(monkeypatch):
    _agents(monkeypatch)
    registry = ApprovingRegistry(
        [_report(passed=2, green=True)], plan_script=["nope", "still nope"]
    )
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is False
    # nothing was ever built: no worker file landed
    built = [
        inp["path"] for tool, inp in registry.calls
        if tool == "write_file" and inp["path"] in ("models.py", "app.py")
    ]
    assert built == []


# ── CI after every layer (item 1) ──


def test_deps_land_after_the_scaffold_and_the_suite_runs_per_layer(monkeypatch):
    _agents(monkeypatch)
    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True

    order = [tool for tool, _ in registry.calls]
    writes = [
        i for i, (tool, inp) in enumerate(registry.calls)
        if tool == "write_file" and inp["path"] in ("models.py", "app.py")
    ]
    assert len(writes) == 2
    install = order.index("install_deps")
    # install runs after the scaffold layer's writes, before layer 2
    assert writes[0] < install < writes[1]
    # and the smoke check that follows the scaffold comes after the install
    assert order.index("smoke_check") > install
    # per-layer run (after layer 1 of 2) plus the fix loop's run
    assert order.count("run_tests") == 2


def test_the_layer_suite_state_feeds_the_next_layer(monkeypatch):
    _agents(monkeypatch)
    seen_states = []

    def spy(gw, task, *a, **k):
        seen_states.append(k.get("test_state", ""))
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run", spy)
    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True

    # first layer: nothing has run yet; second layer carries the green feed
    assert seen_states[0] == ""
    feed = seen_states[-1]
    assert "<test_state>" in feed
    assert "known_green: t0" in feed  # the case that passed after layer 1
    assert "regressions: none" in feed


def test_a_failed_smoke_check_gets_one_repair_then_the_suite_decides(monkeypatch):
    """A syntax error used to end the run on the spot. Now the developer who
    wrote the files gets one short repair pass; a still-failing check is
    recorded and the build continues — the suite gives the honest verdict."""
    _agents(monkeypatch)
    built = []

    def worker_spy(gw, task, *a, **k):
        built.append(task.id)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run", worker_spy)

    class BrokenSmokeRegistry(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "smoke_check":
                self.calls.append((tool, tool_input))
                raise RuntimeError("SyntaxError: invalid syntax")
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = BrokenSmokeRegistry([_report(passed=1, failed=1)] * 6)
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is False
    assert events.events[-1].kind == "run_finished"
    # each task was built, then repaired once after its smoke check failed
    assert built == ["t1", "t1", "t2", "t2"]
    order = [tool for tool, _ in registry.calls]
    assert order.count("smoke_check") == 4  # check, re-check after the repair, per task
    assert "run_tests" in order  # the suite still decided
    assert any("smoke check still fails" in note for note in orch._run_notes)
    assert "README.md" in [inp.get("path") for tool, inp in registry.calls if tool == "write_file"]


def test_a_declined_smoke_check_keeps_the_run_moving(monkeypatch):
    _agents(monkeypatch)

    class DeniedSmokeRegistry(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "smoke_check":
                self.calls.append((tool, tool_input))
                raise ToolDenied("the user declined the smoke check")
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = DeniedSmokeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True
    order = [tool for tool, _ in registry.calls]
    assert order.count("smoke_check") == 2  # both layers asked; both declined


def test_agentic_layers_pass_dependency_references_not_contents(monkeypatch):
    _agents(monkeypatch)
    seen = {}

    async def fake_planner_agentic(gw, spec, notes, call_tool=None, **_k):
        return GRAPH

    async def fake_run_agentic(gw, task, spec, call_tool, **kwargs):
        seen.update(kwargs)
        return _work(task.id, task.owned_paths[0])

    async def fake_reporter_agentic(*a, **k):
        return BUNDLE

    async def fake_reviewer_agentic(*a, **k):
        return CLEAR_REVIEW

    monkeypatch.setattr(orchestrator.planner, "run_agentic", fake_planner_agentic)
    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake_run_agentic)
    monkeypatch.setattr(orchestrator.reporter, "run_agentic", fake_reporter_agentic)
    monkeypatch.setattr(orchestrator.reviewer, "run_agentic", fake_reviewer_agentic)

    settings = Settings(
        _env_file=None,
        agentic_devs=True,
        ask_requirements=False,
        sandbox="host",
        max_parallel_devs=2,
    )
    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = Orchestrator(
        FakeGateway(), registry, events.send, ceiling=200_000, settings=settings
    )
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True
    # the last layer's worker: dependency contents are not inlined, the path is
    assert seen["current_files"] is None
    assert seen["dependency_paths"] == ["models.py"]
    assert "<test_state>" in seen["test_state"]


def test_a_failed_worker_never_leaves_its_sibling_running_detached(monkeypatch):
    """A failing developer must not leave its sibling running detached or end
    the build: the task is retried, failed over to the teammate, then recorded
    as not delivered — while the sibling's work is kept and every lease is
    released."""
    from kiln.agents.worker import WorkerError

    finished = []
    attempts = []

    async def fake_run_agentic(gw, task, spec, call_tool, **kwargs):
        if task.id == "a":
            attempts.append(kwargs.get("agent_id"))
            raise WorkerError("step cap (30) reached without a valid submit_result")
        await asyncio.sleep(0.2)
        finished.append(task.id)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake_run_agentic)
    settings = Settings(_env_file=None, agentic_devs=True, sandbox="host", max_parallel_devs=2)
    registry = FakeRegistry([])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, ceiling=200_000, settings=settings)
    layer = [
        Task(id="a", title="Backend seams", description="x", owned_paths=["a.py"]),
        Task(id="b", title="Frontend shell", description="y", owned_paths=["b.py"]),
    ]
    results = asyncio.run(orch._run_layer("run-1", layer, 0, SPEC, SUITE, {}, GRAPH, ""))
    assert [r.task_id for r in results] == ["b"]  # the sibling's work is kept
    assert finished == ["b"]
    # a: first attempt + same-lane retry on one developer, then one failover attempt
    assert len(attempts) == 3 and len(set(attempts)) == 2
    assert "a" in orch._failed_tasks and "step cap" in orch._failed_tasks["a"]
    claims = [tool for tool, _ in registry.calls].count("claim_scope")
    assert [tool for tool, _ in registry.calls].count("release_scope") == claims == 4


# ── discovery without a mechanical readiness gate; brief review (item 13) ──


def test_partial_discovery_does_not_trigger_orchestrator_ask_user(monkeypatch):
    """Agent-driven clarify: no checklist gate re-asks missing ## headings."""
    _agents(monkeypatch)
    partial = "## Auth model\n- real sessions\n"
    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)],
        files={"docs/discovery.md": partial},
    )
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "an app", [])) is True
    assert [tool for tool, _ in registry.calls].count("ask_user") == 0
    # discovery left as requirements wrote it — orchestrator does not patch headings
    assert registry.files.get("docs/discovery.md") == partial


def test_empty_discovery_does_not_trigger_orchestrator_ask_user(monkeypatch):
    _agents(monkeypatch)
    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)],
        files={},
    )
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "an app", [])) is True
    assert [tool for tool, _ in registry.calls].count("ask_user") == 0


def test_evidence_backed_review_gaps_reopen_the_fixer_once(monkeypatch):
    _agents(monkeypatch)
    seen_gaps = []

    def spy_fixer(gw, task, *a, **k):
        seen_gaps.append(k.get("known_gaps", []))
        return _patch(task.owned_paths[0])

    monkeypatch.setattr(orchestrator.fixer, "run", spy_fixer)
    # the reviewer files the gap once, then clears on the post-fix re-review
    # (a real fix closes the gap; the second verdict is what lets the run go green)
    verdicts = [
        ReviewVerdict(
            gaps=[
                ReviewGap(
                    claim="user accounts",
                    evidence="only localStorage, no server check",
                    paths=["app/auth.js"],
                )
            ],
            summary="1 gap",
        ),
        CLEAR_REVIEW,
    ]
    monkeypatch.setattr(
        orchestrator.reviewer,
        "run",
        lambda *a, **k: verdicts.pop(0) if verdicts else CLEAR_REVIEW,
    )
    registry = FakeRegistry(
        [
            _report(passed=1, failed=1),  # per-layer run after the scaffold
            _report(passed=2, green=True),  # round 1: green, review runs
            _report(passed=2, green=True),  # review round: green but must patch first
            _report(passed=2, green=True),  # after the review patch
        ]
    )
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is True
    # the review round patched exactly once, with the gap as its brief
    assert len(seen_gaps) == 1
    assert any("brief-review" in gap for gap in seen_gaps[0])
    assert any("only localStorage" in gap for gap in seen_gaps[0])
    kinds = [event.kind for event in events.events]
    assert kinds.count("test_run_finished") == 4  # per-layer + 3 fix-loop runs
    # the reporter received the verdict it must report
    reporter_bundle = [
        inp for tool, inp in registry.calls if tool == "write_file"
        and inp["path"] == "README.md"
    ]
    assert len(reporter_bundle) == 1


def test_a_persistent_brief_gap_after_the_fix_round_ends_not_green(monkeypatch):
    """B7 re-verification: the suite going green after the fix round does not
    prove the brief gap closed (the suite never covered it). If the re-review
    still sees the gap, the run is honestly not green — run-9f836de2 shipped an
    unmounted app precisely because no one re-checked the fixer's green."""
    _agents(monkeypatch)
    # the reviewer files the same gap on every call — the "fix" never closes it
    monkeypatch.setattr(
        orchestrator.reviewer,
        "run",
        lambda *a, **k: ReviewVerdict(
            gaps=[
                ReviewGap(
                    claim="served app at /app",
                    evidence="main.jsx never mounts App; no /app route",
                    paths=["frontend/src/main.jsx"],
                )
            ],
            summary="app not mounted",
        ),
    )
    registry = FakeRegistry(
        [
            _report(passed=1, failed=1),  # per-layer
            _report(passed=2, green=True),  # green → review runs
            _report(passed=2, green=True),  # review round: green, must patch
            _report(passed=2, green=True),  # after the patch — re-review still gaps
        ]
    )
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is False
    assert events.events[-1].green is False
    # the reviewer was marked stuck on the re-review, not done
    reviewer_statuses = [
        e for e in events.events
        if e.kind == "agent_status" and getattr(e.agent, "id", None) == "reviewer"
    ]
    assert any(s.status == "stuck" for s in reviewer_statuses)


def test_a_blocked_browser_gate_vetoes_a_ui_run_green(monkeypatch):
    """B5: run-9f836de2's browser gate silently returned [] (pass) with no
    Playwright installed, so a UI run went green on an unverified UI. A
    BLOCKED_HOST_SETUP gate is not fixer-fixable — it must veto the green."""
    _agents(monkeypatch)

    class BlockedBrowserRegistry(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "browser_verify":
                self.calls.append((tool, tool_input))
                return json.dumps({
                    "ok": False,
                    "gaps": [
                        "browser_verify: blocked_host_setup — Playwright is not "
                        "installed on this host, so the UI was never exercised"
                    ],
                })
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = BlockedBrowserRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = _orch(registry, events)
    # a UI idea so _ui_product is true and the browser gate is required
    green = asyncio.run(orch.run("run-1", "a react dashboard app with a UI", []))
    assert green is False
    assert events.events[-1].green is False
    # the browser gate ran (and was blocked), and no fixer round tried to "fix" it
    assert "browser_verify" in [tool for tool, _ in registry.calls]
    reviewer_statuses = [
        e for e in events.events
        if e.kind == "agent_status" and getattr(e.agent, "id", None) == "reviewer"
    ]
    assert any(s.status == "stuck" for s in reviewer_statuses)


def test_smoke_paths_come_from_contract_page_routes():
    """Item 6: the runtime smoke must GET the app-shell routes, not just `/`.
    run-9f836de2 smoked only `/` (Landing, 200) while `/app` 404'd unchecked."""
    graph_with_routes = TaskGraph.model_validate(
        {
            "spec_title": "App",
            "tasks": [{"id": "t1", "title": "t", "description": "d", "owned_paths": ["app.py"]}],
            "contract": {
                "routes": [
                    {"method": "GET", "path": "/"},
                    {"method": "GET", "path": "/app"},
                    {"method": "GET", "path": "/dashboard"},
                    {"method": "GET", "path": "/api/bots"},          # API: skipped
                    {"method": "POST", "path": "/app/comments"},     # not GET: skipped
                    {"method": "GET", "path": "/bots/{bot_id}"},     # param: skipped
                ]
            },
        }
    )
    paths = Orchestrator._smoke_paths(graph_with_routes)
    assert paths == ["/", "/app", "/dashboard"]
    # no contract → just the root, exactly as before
    assert Orchestrator._smoke_paths(GRAPH) == ["/"]
    assert Orchestrator._smoke_paths(None) == ["/"]


def test_product_gates_pass_contract_routes_to_runtime_smoke(monkeypatch):
    _agents(monkeypatch)
    seen_smoke_paths = []

    class SmokeSpyRegistry(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "runtime_smoke":
                self.calls.append((tool, tool_input))
                seen_smoke_paths.extend(tool_input.get("paths") or [])
                return json.dumps({"ok": True, "gaps": []})
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    # give the graph a page route the smoke must hit
    graph_with_app = TaskGraph.model_validate(
        {
            "spec_title": "Tasks",
            "tasks": [
                {"id": "t1", "title": "Models", "description": "d",
                 "owned_paths": ["models.py"]},
                {"id": "t2", "title": "API", "description": "d",
                 "depends_on": ["t1"], "owned_paths": ["app.py"]},
            ],
            "contract": {"routes": [{"method": "GET", "path": "/app"}]},
        }
    )
    monkeypatch.setattr(orchestrator.planner, "run", lambda *a, **k: graph_with_app)
    registry = SmokeSpyRegistry([_report(passed=1, failed=1), _report(passed=2, green=True)])
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "a react dashboard app with a UI", [])) is True
    assert "/app" in seen_smoke_paths


def test_a_run_on_the_fallback_route_says_so_up_front(green_setup):
    """run-9f836de2: 86% of calls were on the fallback route with no signal.
    The run must name the coding roles on fallback (and the lever) up front."""
    orch, registry, events = green_setup
    # TEST_SETTINGS has no open_weight_base_url → coding roles fall back
    asyncio.run(orch.run("run-1", "a to-do app", []))
    notes = [
        e for e in events.events
        if e.kind == "message_sent" and "fallback" in e.summary.lower()
    ]
    assert notes, "expected a routing note naming the fallback route"
    assert "open_weight_base_url" in notes[0].summary
    assert "worker" in notes[0].summary


def test_no_fallback_note_when_the_open_weight_endpoint_is_set(monkeypatch):
    _agents(monkeypatch)
    settings = Settings(
        _env_file=None,
        agentic_devs=False,
        ask_requirements=False,
        sandbox="host",
        max_parallel_devs=2,
        open_weight_base_url="https://example.baseten.co/v1/",
        open_weight_api_key="open-key",
    )
    registry = FakeRegistry([_report(passed=1, failed=1), _report(passed=2, green=True)])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, ceiling=200_000, settings=settings)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True
    notes = [
        e for e in events.events
        if e.kind == "message_sent" and "fallback" in e.summary.lower()
    ]
    assert notes == []


def test_ui_runs_provision_the_browser_before_the_verify_gate(monkeypatch):
    """PRP 5.1: for a UI product, the orchestrator must try to provision the
    browser runtime before the verify gate — a fresh host gets a real browser
    check instead of a permanent BLOCKED."""
    _agents(monkeypatch)
    provision_calls = []

    class ProvisionRegistry(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "ensure_browser_runtime":
                self.calls.append((tool, tool_input))
                provision_calls.append(tool)
                return json.dumps({
                    "ready": True,
                    "playwright_version": "1.49.0",
                    "chromium_version": "131.0",
                    "installed": ["chromium"],
                    "note": "provisioned: chromium",
                })
            if tool == "browser_verify":
                self.calls.append((tool, tool_input))
                return json.dumps({"ok": True, "gaps": []})
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = ProvisionRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "a react dashboard app with a UI", [])) is True
    order = [tool for tool, _ in registry.calls]
    # provisioning ran, and it ran before the verify gate
    assert "ensure_browser_runtime" in order
    assert "browser_verify" in order
    assert order.index("ensure_browser_runtime") < order.index("browser_verify")


def test_a_not_ready_browser_runtime_does_not_crash_the_gates(monkeypatch):
    """A declined/failed install must not crash the gates — the verify gate
    then reports BLOCKED (and the run is honestly not green for a UI product)."""
    _agents(monkeypatch)

    class NotReadyRegistry(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "ensure_browser_runtime":
                self.calls.append((tool, tool_input))
                return json.dumps({"ready": False, "note": "playwright package not installed"})
            if tool == "browser_verify":
                self.calls.append((tool, tool_input))
                return json.dumps({
                    "ok": False,
                    "gaps": ["browser_verify: blocked_host_setup — Playwright is not installed"],
                })
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = NotReadyRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a react dashboard app with a UI", []))
    assert green is False  # blocked browser vetoes the UI run's green


def test_the_review_does_not_run_when_the_suite_never_greens(monkeypatch):
    _agents(monkeypatch)
    review_calls = []
    monkeypatch.setattr(
        orchestrator.reviewer,
        "run",
        lambda *a, **k: review_calls.append(1) or CLEAR_REVIEW,
    )
    registry = FakeRegistry([_report(passed=1, failed=1)] * 6)
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is False
    assert review_calls == []  # no brief review on a broken suite


# ── Milestone A regressions (run-9f836de2) ──


def test_a_docs_write_failure_does_not_flip_a_green_run(monkeypatch):
    """run-9f836de2: the reporter's README write hit the read-before-write
    ledger and the unhandled ExecutionError failed a 17/17 green run. A docs
    defect must never change the suite's verdict."""
    _agents(monkeypatch)

    class FlakyDocsRegistry(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "write_file" and tool_input.get("path") == "README.md":
                self.calls.append((tool, tool_input))
                raise RuntimeError(
                    "ExecutionError: read README.md with read_file before you change it"
                )
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = FlakyDocsRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)]
    )
    events = Collector()
    orch = _orch(registry, events)
    green = asyncio.run(orch.run("run-1", "a to-do app", []))
    assert green is True  # the suite's verdict stands despite the docs crash
    assert events.events[-1].green is True
    # the reporter was marked stuck, not done, so the failure is visible
    reporter_statuses = [
        e for e in events.events
        if e.kind == "agent_status" and getattr(e.agent, "id", None) == "reporter"
    ]
    assert any(s.status == "stuck" for s in reporter_statuses)


def test_write_files_reads_each_target_before_overwriting(monkeypatch):
    """The ReadLedger refuses an overwrite the writer never read. write_files
    must satisfy it by reading each existing target under the same scope first,
    so orchestrator-driven writes (contract, README, patch) cannot crash."""
    _agents(monkeypatch)
    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)],
        files={"README.md": "# old readme\n", "docs/contract.md": "old contract\n"},
    )
    events = Collector()
    orch = _orch(registry, events)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True

    order = [(tool, inp.get("path")) for tool, inp in registry.calls
             if tool in ("read_file", "write_file")]
    # every reporter doc write is preceded by a read of that same path
    for target in ("README.md", "docs/run-summary.md", "docs/cost-report.md"):
        write_idx = next(
            i for i, (tool, path) in enumerate(order)
            if tool == "write_file" and path == target
        )
        assert any(
            tool == "read_file" and path == target for tool, path in order[:write_idx]
        ), f"no read_file before write_file for {target}"


def test_loop_driven_agent_calls_are_recorded_once(monkeypatch):
    """run-9f836de2's transcript showed pairs of identical tool calls: the loop
    logged each call and Orchestrator.call_tool logged it again. Loop-driven
    closures pass record=False, so each call lands in the transcript exactly once."""
    from kiln.agents.transcript import TranscriptLog

    transcript = TranscriptLog()
    registry = FakeRegistry([], files={"a.py": "contents\n"})
    events = Collector()
    orch = Orchestrator(
        FakeGateway(), registry, events.send, ceiling=200_000,
        settings=TEST_SETTINGS, transcript=transcript,
    )

    async def scenario():
        from kiln.agents.roles import Role

        scope = orch._scope("worker-1", "Worker 1", Role.WORKER, ["app.py"])
        # the loop-fed closure (what _run_layer hands to run_agent_loop)
        async def call_tool(name, args):
            return await orch.call_tool("run-1", name, args, agent=scope, record=False)
        # simulate the loop logging the call itself, then delegating
        transcript.record(
            "worker-1", "worker", "tool_call",
            {"name": "read_file", "args": {"path": "a.py"}},
        )
        await call_tool("read_file", {"path": "a.py"})
        # an orchestrator-driven call (claim_scope) still records here
        await orch.call_tool("run-1", "claim_scope", {"owned_paths": ["app.py"]}, agent=scope)

    asyncio.run(scenario())
    reads = [
        e for e in transcript.for_agent("worker-1")
        if e.kind == "tool_call" and e.payload.get("name") == "read_file"
    ]
    assert len(reads) == 1  # not two
    claims = [
        e for e in transcript.for_agent("worker-1")
        if e.kind == "tool_call" and e.payload.get("name") == "claim_scope"
    ]
    assert len(claims) == 1
