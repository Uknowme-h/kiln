"""Regression tests for the multi-agent review fixes (docs/multi-agent-fixes-plan.md).

One section per workstream: framing, scope alignment, agent output quality,
token hygiene, parallelism, communication, and per-run project skills.
"""

import asyncio
import contextvars
import json
import time

import pytest
from pydantic import ValidationError

from kiln.agents import planner, requirements, reviewer, test_author, worker
from kiln.agents.briefing import dependency_handoff_block, existing_workspace_block
from kiln.agents.context import prune_tool_results
from kiln.agents.definitions import definition_for
from kiln.agents.loop import _brief_for_reinjection, _history_for_summary, run_agent_loop
from kiln.agents.roles import Role
from kiln.agents.skills import (
    clear_project_skills,
    frameworks_for_root,
    list_skills,
    register_project_skill_texts,
    resolve_skills,
    root_is_api_only,
)
from kiln.companion.executor import Executor
from kiln.companion.protocol import AgentScope
from kiln.config import Settings
from kiln.gateway.client import Completion, ModelToolCall, OpenAITransport
from kiln.graph import orchestrator
from kiln.graph.orchestrator import Orchestrator
from kiln.schemas.patch import FixPatch
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Contract, Task, TaskGraph
from kiln.tools.registry import TOOLS, specs_for
from kiln.tools.schemas import SubmitDev
from tests.test_orchestrator import (
    GRAPH,
    SPEC,
    SUITE,
    TEST_SETTINGS,
    Collector,
    FakeGateway,
    FakeRegistry,
    _agents,
    _report,
    _work,
)


def _criterion(cid: str, then: str = "t") -> dict:
    return {
        "id": cid,
        "story_id": "s1",
        "given": "g",
        "when": "w",
        "then": then,
        "oracle": f"tests/test_{cid}.py — check",
    }


SPEC2 = Spec.model_validate(
    {
        "title": "Notes",
        "summary": "s",
        "stack": "FastAPI + Vite",
        "stack_id": "python",
        "frameworks": ["fastapi", "vite_react"],
        "roots": [
            {"path": "backend", "role": "api", "framework": "fastapi"},
            {"path": "frontend", "role": "web", "framework": "vite_react"},
        ],
        "user_stories": [{"id": "s1", "as_a": "u", "i_want": "w", "so_that": "t"}],
        "acceptance_criteria": [_criterion("c1", "notes save"), _criterion("c2", "export pdf")],
        "file_plan": [{"path": "backend/app/main.py", "purpose": "api"}],
    }
)


class ScriptGateway:
    """Replies from a script; records every call."""

    def __init__(self, script, settings=None):
        self.script = list(script)
        self.calls = []
        self.settings = settings or Settings(
            _env_file=None,
            enable_explore_subagent=False,
            enable_todo_tool=False,
            post_edit_diagnostics=False,
        )

    def complete(self, role, messages, **kwargs):
        self.calls.append({"role": role, "messages": [dict(m) for m in messages], **kwargs})
        step = self.script.pop(0)
        if isinstance(step, Completion):
            return step
        return Completion(content=step, model="m", prompt_tokens=1, completion_tokens=1)


def _tool_turn(name, args, call_id="c1"):
    return Completion(
        content="",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        tool_calls=(ModelToolCall(id=call_id, name=name, arguments=json.dumps(args)),),
    )


# ── WS1: no v1 framing; the analyst can see an existing project ──────────────


def test_the_analyst_opening_has_no_first_version_frame_or_checklist():
    messages = requirements._base_messages("a notes app", ())
    opening = messages[1]["content"]
    assert "changes the first version" not in opening
    assert "persistence, accounts" not in opening
    # the only mention of a first version is the instruction not to assume one
    assert opening.lower().count("first version") == 1
    assert "do not assume a first version" in opening


def test_the_analyst_gets_read_only_workspace_tools():
    tools = definition_for(Role.REQUIREMENTS).tools
    assert {"list_dir", "glob", "grep", "read_file"} <= set(tools)
    assert "shell" not in tools and "edit_file" not in tools


def test_the_clarify_phase_offers_read_file():
    gateway = ScriptGateway(["no questions needed", json.dumps(SPEC2.model_dump())])

    async def call_tool(name, args):
        raise AssertionError("no tool expected")

    asyncio.run(requirements.run_async(gateway, "a notes app", call_tool=call_tool))
    names = {tool["function"]["name"] for tool in gateway.calls[0]["tools"]}
    assert {"read_file", "list_dir", "glob"} <= names


def test_an_existing_workspace_is_announced_to_the_analyst():
    messages = requirements._base_messages("add tagging", (), ["backend/", "README.md"])
    content = messages[1]["content"]
    assert "<existing_workspace>" in content
    assert "- backend/" in content
    assert "not a rebuild" in content
    assert existing_workspace_block([]) == ""


def test_ask_user_description_is_not_a_fixed_checklist():
    description = TOOLS["ask_user"].description
    assert "auth, persistence" not in description
    assert "genuinely" in description


def test_the_planner_prompt_no_longer_forces_phase_one():
    from kiln.agents.definitions import system_prompt_for

    prompt = system_prompt_for(Role.PLANNER)
    assert "Phase 1 only" not in prompt
    assert "at most five features" not in prompt
    assert "deferred" in prompt


# ── WS2: one scope for plan, suite, reviewer ─────────────────────────────────


def _graph(covers_a=("c1",), deferred=(), covers_b=()):
    return {
        "spec_title": "Notes",
        "tasks": [
            {"id": "a", "title": "A", "description": "d", "owned_paths": ["a.py"],
             "covers": list(covers_a)},
            {"id": "b", "title": "B", "description": "d", "owned_paths": ["b.py"],
             "covers": list(covers_b)},
        ],
        "deferred": list(deferred),
    }


def test_a_tracked_graph_may_not_silently_drop_a_criterion():
    with pytest.raises(ValidationError, match="missing: c2"):
        TaskGraph.model_validate(_graph(), context={"criterion_ids": ["c1", "c2"]})
    ok = TaskGraph.model_validate(
        _graph(deferred=("c2",)), context={"criterion_ids": ["c1", "c2"]}
    )
    assert ok.in_scope(["c1", "c2"]) == ["c1"]


def test_a_criterion_cannot_be_both_built_and_deferred():
    with pytest.raises(ValidationError, match="both covered"):
        TaskGraph.model_validate(_graph(deferred=("c1",)))


def test_deferring_everything_is_rejected():
    with pytest.raises(ValidationError, match="build nothing"):
        TaskGraph.model_validate(
            _graph(covers_a=(), deferred=("c1", "c2")), context={"criterion_ids": ["c1", "c2"]}
        )


def test_a_graph_that_tracks_no_coverage_is_accepted_as_before():
    graph = TaskGraph.model_validate(
        _graph(covers_a=()), context={"criterion_ids": ["c1", "c2"]}
    )
    assert graph.deferred == []


def test_the_planner_accepts_a_sound_graph_whose_coverage_stays_wrong():
    bad = json.dumps(_graph())  # drops c2 twice
    gateway = ScriptGateway([bad, bad])
    graph = planner.run(gateway, SPEC2)
    assert [t.id for t in graph.tasks] == ["a", "b"]
    retry = gateway.calls[1]["messages"][1]["content"]
    assert "covered by a task or listed in deferred" in retry


def test_the_contract_doc_lists_deferred_criteria():
    graph = TaskGraph.model_validate(_graph(deferred=("c2",)))
    doc = planner.render_plan_markdown(SPEC2, graph)
    assert "Deferred to a later run" in doc and "export pdf" in doc


def test_the_test_author_sees_the_contract_and_only_in_scope_criteria():
    suite = {
        "spec_title": "Notes",
        "files": [{"path": "tests/test_notes.py", "content": "def test_x(): ...", "covers": ["c1"]}],
    }
    gateway = ScriptGateway([json.dumps(suite)])
    contract = Contract(routes=[{"method": "POST", "path": "/api/notes"}])
    result = test_author.run(gateway, SPEC2, contract=contract, in_scope=["c1"])
    assert [f.covers for f in result.files] == [["c1"]]  # c2 not required
    user = gateway.calls[0]["messages"][1]["content"]
    assert "/api/notes" in user and "<contract>" in user
    assert "<deferred>\nc2" in user
    assert "export pdf" not in user  # the deferred criterion is not shown as work


def test_a_test_for_a_deferred_criterion_is_rejected():
    suite = {
        "spec_title": "Notes",
        "files": [
            {"path": "tests/test_notes.py", "content": "x", "covers": ["c1"]},
            {"path": "tests/test_pdf.py", "content": "x", "covers": ["c2"]},
        ],
    }
    gateway = ScriptGateway([json.dumps(suite), json.dumps(suite)])
    with pytest.raises(test_author.TestAuthorError):
        test_author.run(gateway, SPEC2, in_scope=["c1"])


def test_the_test_author_gets_framework_and_testing_packs():
    skills = resolve_skills(role="test_author", frameworks=("fastapi",))
    assert "fastapi" in skills and "testing_quality" in skills
    assert "eng_quality" not in skills


def test_the_reviewer_is_told_deferred_criteria_are_not_gaps():
    report = TestReport(cases=[TestCaseResult(nodeid="t", outcome="passed")])
    messages = reviewer._messages("idea", SPEC2, "", "", report, {"a.py": "x"}, None, deferred=["c2"])
    user = messages[1]["content"]
    assert "<deferred>" in user and "c2: export pdf" in user
    assert "never report a\n   criterion listed in <deferred>" in messages[0]["content"]


def test_the_agentic_reviewer_gets_a_path_index_not_bodies():
    body = reviewer._body(
        "idea", SPEC2, "", "", TestReport(), {}, "go", paths=["backend/app/main.py"]
    )
    assert "- backend/app/main.py" in body
    assert "<file path=" not in body


# ── WS3: output quality ───────────────────────────────────────────────────────


def test_skills_are_scoped_to_the_task_root():
    assert frameworks_for_root(SPEC2, "backend") == ("fastapi",)
    assert frameworks_for_root(SPEC2, "frontend") == ("vite_react",)
    assert frameworks_for_root(SPEC2, "") == ("fastapi", "vite_react")
    assert root_is_api_only(SPEC2, "backend") and not root_is_api_only(SPEC2, "frontend")

    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, TEST_SETTINGS)
    orch._spec = SPEC2
    orch._frameworks = ("fastapi", "vite_react")
    orch._ui_product = True
    backend = orch._skill_kwargs(Role.WORKER, root="backend")
    assert "fastapi" in backend["extra_skills"] and "vite_react" not in backend["extra_skills"]
    assert "frontend_quality" not in backend["extra_skills"]
    assert backend["ui_product"] is False
    frontend = orch._skill_kwargs(Role.WORKER, root="frontend")
    assert "vite_react" in frontend["extra_skills"] and frontend["ui_product"] is True


def test_compaction_summarizes_history_not_the_brief_and_keeps_the_newest():
    history = [{"role": "assistant", "content": f"step {i} " + "x" * 2_000} for i in range(60)]
    text = _history_for_summary(history, limit=10_000)
    assert "step 59" in text and "step 0 " not in text
    assert text.startswith("…[")
    brief = "B" * 20_000
    assert _brief_for_reinjection(brief) == brief  # survives whole under the cap


def test_compaction_reinjects_the_brief_intact():
    brief = "<contract>" + "c" * 14_000 + "</contract>"
    seen = []

    class GW:
        n = 0

        def complete(self, role, messages, **kwargs):
            seen.append((kwargs.get("agent_id"), [dict(m) for m in messages]))
            if str(kwargs.get("agent_id", "")).endswith("/compact"):
                return Completion(content="## Objective\nx", model="m", prompt_tokens=1, completion_tokens=1)
            GW.n += 1
            if GW.n <= 2:
                return _tool_turn("read_file", {"path": f"f{GW.n}.py"}, f"r{GW.n}")
            return _tool_turn("submit_result", {
                "task_id": "t1", "summary": "s", "files_written": ["a.py"],
                "what_works": "w", "known_gaps": []})

    async def call_tool(name, args):
        return "body " * 120

    # the threshold counts the history after the brief: two reads cross it
    asyncio.run(run_agent_loop(
        gateway=GW(), role=Role.WORKER, system="sys", user=brief,
        tools=specs_for(("read_file", "submit_result")), call_tool=call_tool,
        finish=SubmitDev, max_steps=5, compact_threshold_chars=1_200,
    ))
    compact = [msgs for aid, msgs in seen if str(aid).endswith("/compact")]
    assert compact
    assert brief not in compact[0][1]["content"]  # the summarizer reads history only
    after = [msgs for aid, msgs in seen if not str(aid).endswith("/compact")][-1]
    assert brief in after[1]["content"]  # not clipped to 6k


def test_pruning_waits_until_enough_is_reclaimable():
    def history():
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        for i in range(6):
            msgs.append({"role": "assistant", "content": "", "tool_calls": []})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 1_000})
        return msgs

    small = history()
    assert prune_tool_results(small, protect_chars=0, protect_turns=1, min_reclaim_chars=20_000) == 0
    assert prune_tool_results(history(), protect_chars=0, protect_turns=1, min_reclaim_chars=0) == 5


def test_the_worker_brief_carries_plan_memo_and_dependency_handoff():
    task = Task(id="t2", title="API", description="d", owned_paths=["app.py"], depends_on=["t1"])
    gateway = ScriptGateway([
        _tool_turn("submit_result", {"task_id": "t2", "summary": "s", "files_written": ["app.py"],
                                     "what_works": "w", "known_gaps": []}),
    ])

    async def call_tool(name, args):
        return "# code"

    asyncio.run(worker.run_agentic(
        gateway, task, SPEC2, call_tool,
        plan_memo="DOM id #note-list is frozen",
        handoffs=[{"task_id": "t1", "summary": "models", "what_works": "crud",
                   "known_gaps": ["no delete yet"]}],
        retry_note="step cap reached",
    ))
    user = gateway.calls[0]["messages"][1]["content"]
    assert "<plan_memo>\nDOM id #note-list is frozen" in user
    assert "<dependency_handoff>" in user and "no delete yet" in user
    assert "<previous_attempt>" in user and "step cap reached" in user


def test_the_worker_prompt_treats_the_suite_as_a_floor():
    from kiln.agents.definitions import system_prompt_for

    prompt = system_prompt_for(Role.WORKER)
    assert "Smallest change that satisfies the suite" not in prompt
    assert "The suite is a floor" in prompt
    assert "<spec_excerpt>" in prompt


# ── existing projects: inventory, protection ─────────────────────────────────


def test_an_existing_project_is_announced_and_user_files_are_never_clobbered(monkeypatch):
    _agents(monkeypatch)
    seen = {}

    async def fake_requirements(gw, idea, notes, **kwargs):
        seen["inventory"] = kwargs.get("inventory")
        return SPEC

    monkeypatch.setattr(orchestrator.requirements, "run_async", fake_requirements)
    registry = FakeRegistry(
        [_report(passed=1, failed=1), _report(passed=2, green=True)],
        files={"README.md": "# mine\n", "tests/test_x.py": "def test_mine(): ...\n"},
        inventory=["README.md", "tests/", ".git/"],
    )
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("run-1", "add tags", [])) is True
    assert seen["inventory"] == ["README.md", "tests/"]  # .git ignored
    written = [inp["path"] for tool, inp in registry.calls if tool == "write_file"]
    assert "tests/test_x.py" not in written and "tests/test_x_kiln.py" in written
    assert "README.md" not in written and "docs/kiln-readme.md" in written
    # blame still maps the renamed file to its criterion
    assert orch._suite.files[0].path == "tests/test_x_kiln.py"


def test_a_fresh_workspace_keeps_writing_readme(green_fresh):
    registry = green_fresh
    written = [inp["path"] for tool, inp in registry.calls if tool == "write_file"]
    assert "README.md" in written


@pytest.fixture()
def green_fresh(monkeypatch):
    _agents(monkeypatch)
    registry = FakeRegistry([_report(passed=1, failed=1), _report(passed=2, green=True)])
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True
    return registry


def test_the_plan_approval_shows_deferred_scope(monkeypatch):
    _agents(monkeypatch)
    spec = SPEC.model_copy(update={"acceptance_criteria": [
        SPEC.acceptance_criteria[0],
        SPEC.acceptance_criteria[0].model_copy(update={"id": "c9", "then": "export to pdf"}),
    ]})

    async def fake_requirements(*_a, **_k):
        return spec

    graph = TaskGraph.model_validate({
        "spec_title": "Tasks",
        "tasks": [{"id": "t1", "title": "M", "description": "d", "owned_paths": ["models.py"],
                   "covers": ["c1"]}],
        "deferred": ["c9"],
    })
    monkeypatch.setattr(orchestrator.requirements, "run_async", fake_requirements)
    monkeypatch.setattr(orchestrator.planner, "run", lambda *a, **k: graph)
    seen = {}

    def fake_author(gw, spec, **kwargs):
        seen["in_scope"] = kwargs.get("in_scope")
        return SUITE

    monkeypatch.setattr(orchestrator.test_author, "run", fake_author)
    registry = FakeRegistry([_report(passed=2, green=True)])
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    asyncio.run(orch.run("run-1", "tasks", []))
    lines = next(inp["lines"] for tool, inp in registry.calls if tool == "confirm_plan")
    assert any("deferred to a later run" in line for line in lines)
    assert any("c9: export to pdf" in line for line in lines)
    assert seen["in_scope"] == ["c1"]
    assert orch._deferred == ["c9"]


# ── WS5: parallelism ─────────────────────────────────────────────────────────


def _agentic_settings(**overrides):
    return Settings(
        _env_file=None, agentic_devs=True, ask_requirements=False, sandbox="host",
        max_parallel_devs=2, **overrides,
    )


def test_a_ready_task_starts_without_waiting_for_an_unrelated_slow_one(monkeypatch):
    """s → {slow, b}; b → c. c must start while slow is still running (the old
    layers held c until every depth-1 task, including slow, had finished)."""
    graph = TaskGraph.model_validate({
        "spec_title": "T",
        "tasks": [
            {"id": "s", "title": "S", "description": "d", "owned_paths": ["s.py"], "kind": "scaffold"},
            {"id": "slow", "title": "Slow", "description": "d", "owned_paths": ["slow.py"], "depends_on": ["s"]},
            {"id": "b", "title": "B", "description": "d", "owned_paths": ["b.py"], "depends_on": ["s"]},
            {"id": "c", "title": "C", "description": "d", "owned_paths": ["c.py"], "depends_on": ["b"]},
        ],
    })
    timeline = {}

    async def fake_run_agentic(gw, task, spec, call_tool, **kwargs):
        timeline[f"{task.id}-start"] = time.monotonic()
        await asyncio.sleep(0.4 if task.id == "slow" else 0.02)
        timeline[f"{task.id}-end"] = time.monotonic()
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake_run_agentic)
    registry = FakeRegistry([_report(passed=1, failed=1)] * 5)
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, _agentic_settings())
    asyncio.run(orch._build("run-1", SPEC, SUITE, graph, ""))
    assert timeline["c-start"] < timeline["slow-end"]
    assert set(orch._results_by_task) == {"s", "slow", "b", "c"}


def test_a_failed_worker_is_retried_once_with_the_reason(monkeypatch):
    calls = []

    async def flaky(gw, task, spec, call_tool, **kwargs):
        calls.append(kwargs.get("retry_note", ""))
        if len(calls) == 1:
            raise worker.WorkerError("the agent stopped without submitting a result")
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", flaky)
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, _agentic_settings())
    task = GRAPH.tasks[0]
    result = asyncio.run(orch._run_task("run-1", task, SPEC, SUITE, {}, GRAPH, "", ""))
    assert result.task_id == task.id
    assert calls[0] == "" and "stopped without submitting" in calls[1]


def test_worker_retries_can_be_turned_off(monkeypatch):
    async def boom(*_a, **_k):
        raise worker.WorkerError("nope")

    monkeypatch.setattr(orchestrator.worker, "run_agentic", boom)
    registry = FakeRegistry([])
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1,
                        _agentic_settings(worker_retries=0))
    with pytest.raises(orchestrator.LoopError, match=r"failed: nope"):
        asyncio.run(orch._run_task("run-1", GRAPH.tasks[0], SPEC, SUITE, {}, GRAPH, "", ""))
    assert [t for t, _ in registry.calls].count("claim_scope") == 1


def _fixer_counter(monkeypatch):
    seen = []

    def fake_fixer(gw, task, report, *a, **k):
        seen.append({"task": task, "report": report, "escalate": k.get("escalate")})
        return FixPatch.model_validate(
            {"diagnosis": f"fix {len(seen)}", "files": [{"path": task.owned_paths[0], "content": "x"}]},
            context={"owned_paths": task.owned_paths},
        )

    monkeypatch.setattr(orchestrator.fixer, "run", fake_fixer)
    return seen


def test_improving_fix_passes_get_the_full_budget(monkeypatch):
    _agents(monkeypatch)
    seen = _fixer_counter(monkeypatch)
    registry = FakeRegistry([
        _report(passed=1, failed=1),  # per-stage run
        _report(failed=4), _report(failed=3), _report(failed=2), _report(failed=1),
    ])
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is False
    assert len(seen) == 3  # max_fix_attempts passes, not 2
    assert [s["escalate"] for s in seen] == [False, False, True]


def test_a_stalled_fix_loop_stops_and_escalates_after_the_stall(monkeypatch):
    _agents(monkeypatch)
    seen = _fixer_counter(monkeypatch)
    registry = FakeRegistry([_report(passed=1, failed=1)] + [_report(failed=2)] * 6)
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is False
    assert len(seen) == 2
    assert [s["escalate"] for s in seen] == [False, True]


MULTIROOT_GRAPH = TaskGraph.model_validate({
    "spec_title": "Notes",
    "tasks": [
        {"id": "api", "title": "API", "description": "d", "root": "backend",
         "owned_paths": ["backend/app/notes.py"], "covers": ["c1"]},
        {"id": "ui", "title": "UI", "description": "d", "root": "frontend",
         "owned_paths": ["frontend/src/Notes.tsx"], "covers": ["c2"]},
    ],
})
MULTIROOT_SUITE = TestSuite.model_validate({
    "spec_title": "Notes",
    "files": [
        {"path": "tests/test_api.py", "content": "x", "covers": ["c1"]},
        {"path": "tests/test_ui.py", "content": "x", "covers": ["c2"]},
    ],
})


def _orch_for_fix():
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, TEST_SETTINGS)
    orch._suite = MULTIROOT_SUITE
    return orch


def test_failures_that_split_by_root_get_one_fixer_per_root():
    report = TestReport(cases=[
        TestCaseResult(nodeid="tests/test_api.py::test_a", outcome="failed"),
        TestCaseResult(nodeid="tests/test_ui.py::test_b", outcome="failed"),
        TestCaseResult(nodeid="tests/test_ui.py::test_ok", outcome="passed"),
    ])
    groups = _orch_for_fix()._fix_groups(report, MULTIROOT_GRAPH)
    assert sorted(owners[0].id for owners, _ in groups) == ["api", "ui"]
    for owners, group_report in groups:
        failing = [c.nodeid for c in group_report.cases if c.outcome == "failed"]
        assert len(failing) == 1 and owners[0].id in ("api", "ui")


def test_a_failure_spanning_roots_keeps_a_single_fixer():
    graph = TaskGraph.model_validate({
        **MULTIROOT_GRAPH.model_dump(),
        "tasks": [
            {**MULTIROOT_GRAPH.tasks[0].model_dump(), "covers": ["c1"]},
            {**MULTIROOT_GRAPH.tasks[1].model_dump(), "covers": ["c1", "c2"]},
        ],
    })
    report = TestReport(cases=[
        TestCaseResult(nodeid="tests/test_api.py::test_contract", outcome="failed"),
    ])
    groups = _orch_for_fix()._fix_groups(report, graph)
    assert len(groups) == 1
    assert {t.id for t in groups[0][0]} == {"api", "ui"}


def test_parallel_fixers_can_be_turned_off():
    orch = _orch_for_fix()
    orch.settings = Settings(_env_file=None, parallel_fixers=False)
    report = TestReport(cases=[
        TestCaseResult(nodeid="tests/test_api.py::test_a", outcome="failed"),
        TestCaseResult(nodeid="tests/test_ui.py::test_b", outcome="failed"),
    ])
    assert len(orch._fix_groups(report, MULTIROOT_GRAPH)) == 1


# ── WS6: communication ───────────────────────────────────────────────────────


def test_dependents_receive_their_dependencies_handoff(monkeypatch):
    _agents(monkeypatch)
    seen = {}

    def first_has_gaps(gw, task, *a, **k):
        seen[task.id] = k.get("handoffs")
        result = _work(task.id, task.owned_paths[0])
        if task.id == "t1":
            result = result.model_copy(update={"known_gaps": ["no index on title"]})
        return result

    monkeypatch.setattr(orchestrator.worker, "run", first_has_gaps)
    registry = FakeRegistry([_report(passed=1, failed=1), _report(passed=2, green=True)])
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True
    assert seen["t1"] == []
    assert seen["t2"][0]["task_id"] == "t1"
    assert seen["t2"][0]["known_gaps"] == ["no index on title"]


def test_the_fix_handoff_names_the_worker_that_built_the_task(monkeypatch):
    _agents(monkeypatch)
    registry = FakeRegistry([
        _report(passed=1, failed=1), _report(passed=1, failed=1), _report(passed=2, green=True),
    ])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("run-1", "a to-do app", [])) is True
    handoffs = [e for e in events.events if e.kind == "message_sent" and e.to_agent == "fixer"]
    assert handoffs and handoffs[0].from_agent in orch._task_agent.values()


def test_list_peers_reports_the_files_each_peer_wrote(tmp_path):
    from tests.test_executor import AlwaysAllow

    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())
    w1 = AgentScope(id="worker-1", label="Worker 1", role="worker", owned_paths=["app/"])
    executor.execute("claim_scope", {"owned_paths": ["app/"], "task": "api"}, w1)
    executor.execute("write_file", {"path": "app/main.py", "content": "x = 1\n"}, w1)
    executor.execute("edit_file", {"path": "app/main.py", "old_string": "1", "new_string": "2"}, w1)
    peers = json.loads(executor.execute("list_peers", {}, w1))
    assert peers[0]["files"] == ["app/main.py"]


def test_dependency_handoff_block_is_empty_without_handoffs():
    assert dependency_handoff_block([]) == ""


# ── WS4 / WS7: client reuse, per-run project skills ──────────────────────────


def test_the_openai_client_is_reused_per_endpoint():
    transport = OpenAITransport()
    a = transport._client("https://x/v1", "k")
    assert transport._client("https://x/v1", "k") is a
    assert transport._client("https://y/v1", "k") is not a


def test_project_skills_do_not_leak_between_runs():
    def run_a():
        register_project_skill_texts({".kiln/skills/house.md": "# House\nserif"})
        return {s["name"] for s in list_skills()}

    def run_b():
        clear_project_skills()
        return {s["name"] for s in list_skills()}

    names_a = contextvars.copy_context().run(run_a)
    names_b = contextvars.copy_context().run(run_b)
    assert "project:house" in names_a
    assert "project:house" not in names_b
