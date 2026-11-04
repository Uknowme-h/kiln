"""Shared files, parallel planning and consistent quality for two developers.

Real teams finish sooner by slicing work vertically, stubbing interfaces
first, keeping shared files tiny — and, when two people need one file, by an
exclusive checkout: keep working elsewhere until it is released, or wait.
"""

import asyncio
import json

from kiln.companion.executor import Executor
from kiln.companion.protocol import AgentScope
from kiln.config import Settings
from kiln.graph import orchestrator
from kiln.graph.lanes import parallel_estimate
from kiln.graph.orchestrator import Orchestrator
from tests.test_executor import AlwaysAllow
from tests.test_orchestrator import SPEC, SUITE, Collector, FakeGateway, FakeRegistry, _work
from tests.test_two_developers import SPLIT, _agentic, _graph, _spec, _t


def _orch(**overrides):
    return Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, _agentic(**overrides))


# ── checkout: grant, queue, wait, deadlock ───────────────────────────────────


def test_a_free_shared_file_is_granted_and_its_owner_told(monkeypatch):
    graph = _graph(_t("s", ["app/main.py"], kind="scaffold"), _t("notes", ["app/notes.py"], ["s"]))
    replies = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "notes":
            replies["notes"] = await kwargs["on_request_files"](
                ["app/main.py"], "register the notes router", False
            )
            replies["extra"] = kwargs["extra_paths"]()
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPEC, SUITE, {}, graph, "", state=None, smoke=False))
    assert replies["notes"].startswith("granted: app/main.py")
    assert replies["extra"] == ["app/main.py"]
    claim = [inp["owned_paths"] for tool, inp in orch.registry.calls if tool == "claim_scope"]
    assert ["app/notes.py", "app/main.py"] in claim  # the lease was extended
    assert orch._board.borrowed_paths("A") == [] and orch._board.borrowed_paths("B") == []


def test_a_held_file_is_queued_then_handed_over_with_a_message(monkeypatch):
    graph = _graph(_t("api", ["backend/routes.js"], root="backend"),
                   _t("ui", ["frontend/App.tsx"], root="frontend"))
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "ui":
            seen["reply"] = await kwargs["on_request_files"](
                ["backend/routes.js"], "add CORS for the dev server", False
            )
            await asyncio.sleep(0.2)  # keeps working on its own files meanwhile
            seen["inbox"] = kwargs["inbox"]()
            seen["extra"] = kwargs["extra_paths"]()
        else:
            await asyncio.sleep(0.05)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    orch._spec = SPLIT
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, SUITE, {}, graph, "", state=None, smoke=False))
    assert seen["reply"].startswith("queued: backend/routes.js (Dev A (backend) is working on api)")
    assert any("released and added to your lease" in note for note in seen["inbox"])
    assert seen["extra"] == ["backend/routes.js"]


def test_wait_true_pauses_until_the_teammate_releases(monkeypatch):
    graph = _graph(_t("api", ["backend/routes.js"], root="backend"),
                   _t("ui", ["frontend/App.tsx"], root="frontend"))
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "ui":
            await asyncio.sleep(0.01)
            seen["reply"] = await kwargs["on_request_files"](["backend/routes.js"], "CORS", True)
        else:
            await asyncio.sleep(0.15)
            seen["api_done"] = True
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    orch._spec = SPLIT
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, SUITE, {}, graph, "", state=None, smoke=False))
    assert seen["api_done"] and seen["reply"].startswith("now yours: backend/routes.js")


def test_two_developers_can_never_wait_on_each_other(monkeypatch):
    graph = _graph(_t("api", ["backend/routes.js"], root="backend"),
                   _t("ui", ["frontend/App.tsx"], root="frontend"))
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "api":
            seen["api"] = await kwargs["on_request_files"](["frontend/App.tsx"], "x", True)
        else:
            await asyncio.sleep(0.05)
            seen["ui"] = await kwargs["on_request_files"](["backend/routes.js"], "y", True)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch(file_wait_seconds=10)
    orch._spec = SPLIT
    asyncio.run(asyncio.wait_for(
        orch._run_tasks("r", list(graph.tasks), SPLIT, SUITE, {}, graph, "", state=None, smoke=False),
        timeout=5,
    ))
    assert "cannot wait" in seen["ui"]  # the second waiter is refused, no deadlock
    assert seen["api"].startswith("now yours")  # and the first gets the file once ui ends


def test_the_suite_and_kiln_documents_are_never_lent(monkeypatch):
    graph = _graph(_t("a", ["a.py"]))
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        seen["reply"] = await kwargs["on_request_files"](
            ["tests/test_a.py", "README.md", "docs/plan.md", "a.py"], "r", False
        )
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPEC, SUITE, {}, graph, "", state=None, smoke=False))
    assert "tests/test_a.py (the suite is never edited)" in seen["reply"]
    assert "README.md (Kiln's own documents)" in seen["reply"]
    assert "granted" not in seen["reply"]  # a.py is already the task's own file


def test_a_teammates_task_waits_while_its_file_is_checked_out(monkeypatch):
    graph = _graph(
        _t("s", ["app/main.py"], kind="scaffold"),
        _t("one", ["app/one.py"], ["s"]),
        _t("two", ["app/two.py"], ["s"]),
        _t("three", ["app/three.py"], ["two"]),
    )
    events = []

    async def fake(gw, task, spec, call_tool, **kwargs):
        events.append(f"start {task.id}")
        if task.id == "one":
            await kwargs["on_request_files"](["app/three.py"], "shared constant", False)
            await asyncio.sleep(0.15)
        events.append(f"end {task.id}")
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPEC, SUITE, {}, graph, "", state=None, smoke=False))
    # "three" belongs to the other developer and is ready after "two", but its
    # file is checked out by "one": it starts only after "one" releases it
    assert events.index("start three") > events.index("end one")


def test_a_reclaim_for_the_same_task_keeps_the_written_files(tmp_path):
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())
    dev = AgentScope(id="worker-1", label="Dev A", role="worker", owned_paths=["a.py"])
    executor.execute("claim_scope", {"owned_paths": ["a.py"], "task": "T"}, dev)
    executor.execute("write_file", {"path": "a.py", "content": "x"}, dev)
    executor.execute("claim_scope", {"owned_paths": ["a.py", "b.py"], "task": "T"}, dev)
    peers = json.loads(executor.execute("list_peers", {}, dev))
    assert peers[0]["files"] == ["a.py"] and peers[0]["owned_paths"] == ["a.py", "b.py"]


# ── parallel planning ────────────────────────────────────────────────────────


LAYERED = _graph(
    _t("s", ["app/main.py"], kind="scaffold"),
    _t("models", ["app/models.py"], ["s"]),
    _t("routes", ["app/routes.py"], ["models"]),
    _t("auth", ["app/auth.py"], ["routes"]),
)
SLICED = _graph(
    _t("s", ["app/main.py", "app/db.py"], kind="scaffold"),
    _t("notes", ["app/notes.py"], ["s"]),
    _t("tags", ["app/tags.py"], ["s"]),
    _t("auth", ["app/auth.py"], ["s"]),
)


def test_parallel_estimate_tells_a_chain_from_slices():
    assert parallel_estimate(LAYERED, _spec()) == (4, 4)
    assert parallel_estimate(SLICED, _spec()) == (3, 4)
    assert parallel_estimate(SLICED, _spec(), 1) == (4, 4)


def test_a_sequential_plan_is_resplit_once_for_two_developers(monkeypatch):
    calls = []

    def planner(gw, spec, notes, **_k):
        calls.append(notes)
        return SLICED  # the revision; the layered first plan is passed in

    monkeypatch.setattr(orchestrator.planner, "run", planner)
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1,
                        Settings(_env_file=None, agentic_devs=False))
    graph = asyncio.run(orch._parallelize("r", SPEC, (), LAYERED, None))
    assert graph is SLICED
    assert len(calls) == 1 and "fully sequential" in calls[0][-1]
    assert any("parallel re-split" in note for note in orch._run_notes)


def test_a_worse_or_failed_resplit_keeps_the_original(monkeypatch):
    monkeypatch.setattr(orchestrator.planner, "run", lambda *a, **k: LAYERED)
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1,
                        Settings(_env_file=None, agentic_devs=False))
    assert asyncio.run(orch._parallelize("r", SPEC, (), LAYERED, None)) is LAYERED

    def boom(*_a, **_k):
        raise RuntimeError("model down")

    monkeypatch.setattr(orchestrator.planner, "run", boom)
    assert asyncio.run(orch._parallelize("r", SPEC, (), LAYERED, None)) is LAYERED


def test_small_or_single_developer_plans_are_left_alone(monkeypatch):
    monkeypatch.setattr(orchestrator.planner, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    one = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1,
                       Settings(_env_file=None, max_parallel_devs=1))
    assert asyncio.run(one._parallelize("r", SPEC, (), LAYERED, None)) is LAYERED
    two = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, Settings(_env_file=None))
    small = _graph(_t("a", ["a.py"]), _t("b", ["b.py"], ["a"]))
    assert asyncio.run(two._parallelize("r", SPEC, (), small, None)) is small


def test_the_approval_screen_shows_who_builds_what_and_the_speedup():
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, Settings(_env_file=None))
    lines = orch._plan_lines(_spec(), SLICED, "4 tasks:")
    assert "about 3 task-steps instead of 4" in lines[1]
    assert any(line.startswith("notes [feature] · Dev ") for line in lines)


def test_the_planner_is_told_to_slice_for_two_developers():
    from kiln.agents.definitions import system_prompt_for
    from kiln.agents.roles import Role

    prompt = system_prompt_for(Role.PLANNER)
    assert "Slice vertically, not by layer" in prompt
    assert "Never pre-write an" in prompt and "hard dependencies only" in prompt


# ── consistent quality across both developers ────────────────────────────────


def test_team_builds_require_the_same_framework_and_quality_skills(monkeypatch):
    spec = _spec([{"path": "backend", "role": "api", "framework": "express"},
                  {"path": "frontend", "role": "web", "framework": "vite_react"}])
    graph = _graph(_t("api", ["backend/a.js"], root="backend"), _t("ui", ["frontend/b.tsx"], root="frontend"))
    required = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        required[task.id] = list(task.required_skills)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    orch._spec = spec
    asyncio.run(orch._run_tasks("r", list(graph.tasks), spec, SUITE, {}, graph, "", state=None, smoke=False))
    assert required == {"api": ["express", "eng_quality"], "ui": ["vite_react", "eng_quality"]}
