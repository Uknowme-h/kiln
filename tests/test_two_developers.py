"""Two developers in parallel (docs/two-developer-lanes-plan.md).

Lane assignment, the team board and its messages, work stealing, failover,
dependency-failed notes, on-demand skills, peer-message delivery in the agent
loop, shell verification for a shared workspace, and the self-healing suite.
"""

import asyncio
import json
import time

import pytest

from kiln.agents.coding_tools import wrap_coding_tools
from kiln.agents.definitions import system_prompt_for
from kiln.agents.loop import run_agent_loop
from kiln.agents.roles import Role
from kiln.agents.skills import load_skill
from kiln.companion.commandscan import scan_command, write_targets
from kiln.companion.executor import Executor, PermissionDenied, _fingerprint
from kiln.companion.protocol import AgentScope
from kiln.config import Settings
from kiln.gateway.client import Completion, ModelToolCall
from kiln.graph import orchestrator
from kiln.graph.lanes import TeamBoard, assign_lanes
from kiln.graph.orchestrator import Orchestrator
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import TaskGraph
from kiln.tools.registry import specs_for
from kiln.tools.schemas import SubmitDev
from tests.test_executor import AlwaysAllow
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


def _spec(roots=()):
    return Spec.model_validate(
        {
            "title": "N", "summary": "s", "stack": "x", "stack_id": "node",
            "frameworks": sorted({r["framework"] for r in roots}),
            "roots": list(roots),
            "user_stories": [{"id": "s1", "as_a": "u", "i_want": "w", "so_that": "t"}],
            "acceptance_criteria": [{"id": "c1", "story_id": "s1", "given": "g", "when": "w",
                                     "then": "t", "oracle": "tests/a.test.ts — x"}],
            "file_plan": [{"path": "a", "purpose": "p"}],
        }
    )


SPLIT = _spec([
    {"path": "backend", "role": "api", "framework": "express"},
    {"path": "frontend", "role": "web", "framework": "vite_react"},
])


def _graph(*tasks):
    return TaskGraph.model_validate({"spec_title": "N", "tasks": list(tasks)})


def _t(tid, paths, deps=(), root="", kind="feature"):
    return {"id": tid, "title": tid.upper(), "description": "d", "owned_paths": list(paths),
            "depends_on": list(deps), "root": root, "kind": kind}


# ── assignment ───────────────────────────────────────────────────────────────


def test_a_split_product_gives_backend_to_a_and_frontend_to_b():
    graph = _graph(
        _t("be", ["backend/app.js"], root="backend", kind="scaffold"),
        _t("fe", ["frontend/src/App.tsx"], root="frontend", kind="scaffold"),
        _t("api", ["backend/routes.js"], deps=["be"], root="backend"),
        _t("ui", ["frontend/src/Page.tsx"], deps=["fe", "api"], root="frontend"),
    )
    lanes, focus = assign_lanes(graph, SPLIT)
    assert lanes == {"be": "A", "api": "A", "fe": "B", "ui": "B"}
    assert focus == {"A": "backend", "B": "frontend"}


def test_a_single_root_app_still_splits_independent_work():
    graph = _graph(
        _t("s", ["app/layout.tsx"], kind="scaffold"),
        _t("auth", ["lib/auth.ts"], deps=["s"]),
        _t("notes", ["app/notes/page.tsx"], deps=["s"]),
        _t("share", ["app/share/page.tsx"], deps=["notes"]),
    )
    lanes, focus = assign_lanes(graph, _spec())
    assert lanes["s"] == "A" and lanes["auth"] == "A"
    assert lanes["notes"] == "B" and lanes["share"] == "B"  # stays with its dependency's owner
    assert focus == {"A": "shared", "B": "shared"}


def test_a_chain_stays_with_one_developer():
    graph = _graph(_t("a", ["a.py"]), _t("b", ["b.py"], ["a"]), _t("c", ["c.py"], ["b"]))
    lanes, _ = assign_lanes(graph, _spec())
    assert set(lanes.values()) == {"A"}


def test_one_lane_takes_everything():
    graph = _graph(_t("a", ["a.py"]), _t("b", ["b.py"]))
    lanes, focus = assign_lanes(graph, _spec(), lane_count=1)
    assert set(lanes.values()) == {"A"} and focus == {"A": "shared"}


# ── board and messages ───────────────────────────────────────────────────────


def test_the_board_shows_both_developers_and_carries_notes():
    graph = _graph(_t("a", ["a.py"]), _t("b", ["b.py"]))
    board = TeamBoard(graph, _spec())
    assert {lane.agent_id for lane in board.lanes.values()} == {"worker-1", "worker-2"}
    board.start("a", "A")
    reply = board.post("B", "A", "I renamed /api/note to /api/notes")
    assert reply.startswith("delivered to Dev A")
    assert board.drain("A") == ["Dev B: I renamed /api/note to /api/notes"]
    assert board.drain("A") == []
    board.finish("a", "A", summary="models done", files=["a.py"], gaps=["no index"])
    view = board.render_for("A", "b")
    assert "Your teammate is Dev B" in view
    assert "a [feature] A · Dev A · done · models done" in view and "gaps: no index" in view
    assert "<lane_notes>" in view and "renamed /api/note" in view
    # B is idle: the finish note waits on the board instead of an inbox
    assert board.lanes["B"].inbox == [] and any("finished a" in n for n in board.notes)


def test_a_stolen_task_tells_its_owner():
    graph = _graph(_t("a", ["a.py"]), _t("b", ["b.py"]))
    board = TeamBoard(graph, _spec())
    owner = board.tasks["b"].lane
    thief = "A" if owner == "B" else "B"
    board.start("b", thief)
    assert board.tasks["b"].lane == thief
    assert any("took over b" in note for note in board.notes)


# ── scheduling: stealing, failover, failed dependencies ──────────────────────


def _agentic(**overrides):
    values = {"agentic_devs": True, "ask_requirements": False, "sandbox": "host",
              "max_parallel_devs": 2, **overrides}
    return Settings(_env_file=None, **values)


def _orch(settings=None, registry=None):
    return Orchestrator(FakeGateway(), registry or FakeRegistry([_report(passed=1, failed=1)] * 6),
                        Collector().send, 1, settings or _agentic())


def test_an_idle_developer_steals_ready_work_from_a_busy_teammate(monkeypatch):
    graph = _graph(
        _t("slow", ["backend/a.js"], root="backend"),
        _t("api2", ["backend/b.js"], root="backend"),
        _t("ui", ["frontend/c.tsx"], root="frontend"),
    )
    ran_by = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        ran_by[task.id] = kwargs["agent_id"]
        await asyncio.sleep(0.3 if task.id == "slow" else 0.01)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    orch._spec = SPLIT
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, SUITE, {}, graph, "",
                                state=None, smoke=False))
    assert ran_by["slow"] == "worker-1" and ran_by["ui"] == "worker-2"
    assert ran_by["api2"] == "worker-2"  # stolen while Dev A was busy
    assert any("took over api2" in note for note in orch._board.notes)


def test_a_task_fails_over_to_the_teammate(monkeypatch):
    seen = []

    async def fake(gw, task, spec, call_tool, **kwargs):
        seen.append((kwargs["agent_id"], kwargs.get("retry_note", "")))
        if kwargs["agent_id"] == "worker-1":
            raise orchestrator.worker.WorkerError("stuck repeating the same call")
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    graph = _graph(_t("a", ["a.py"]))
    orch = _orch()
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPEC, SUITE, {}, graph, "",
                                state=None, smoke=False))
    assert [agent for agent, _ in seen] == ["worker-1", "worker-1", "worker-2"]
    assert "Dev A could not deliver this task" in seen[-1][1]
    assert orch._board.tasks["a"].status == "done" and "a" not in orch._failed_tasks


def test_a_failed_dependency_does_not_stop_its_dependents(monkeypatch):
    handoffs = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "a":
            raise orchestrator.worker.WorkerError("boom")
        handoffs[task.id] = kwargs.get("handoffs")
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    graph = _graph(_t("a", ["a.py"]), _t("b", ["b.py"], ["a"]))
    orch = _orch()
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPEC, SUITE, {}, graph, "",
                                state=None, smoke=False))
    assert orch._board.tasks["a"].status == "failed"
    assert orch._board.tasks["b"].status == "done"
    assert handoffs["b"][0]["task_id"] == "a" and "NOT DELIVERED" in handoffs["b"][0]["summary"]
    assert any("was not delivered" in note for note in orch._run_notes)


def test_developers_message_each_other_mid_task(monkeypatch):
    graph = _graph(_t("api", ["backend/a.js"], root="backend"),
                   _t("ui", ["frontend/b.tsx"], root="frontend"))
    received = []
    contexts = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        contexts[task.id] = kwargs["team_context"]
        if task.id == "ui":
            await asyncio.sleep(0.05)  # Dev A sends its note meanwhile
            received.extend(kwargs["inbox"]())
        else:
            await asyncio.sleep(0.01)
            reply = await kwargs["on_notify"]("POST /api/notes now returns {id}")
            received.append(f"reply: {reply}")
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    orch = _orch()
    orch._spec = SPLIT
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, SUITE, {}, graph, "",
                                state=None, smoke=False))
    assert "reply: delivered to Dev B (frontend)" in received[0]
    assert "Dev A (backend): POST /api/notes now returns {id}" in received
    assert "Your teammate is Dev B (frontend)" in contexts["api"]


def test_a_single_developer_gets_no_notify_tool(monkeypatch):
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        seen["on_notify"] = kwargs.get("on_notify")
        seen["team"] = kwargs["team_context"]
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    graph = _graph(_t("a", ["a.py"]))
    orch = _orch(_agentic(max_parallel_devs=1))
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPEC, SUITE, {}, graph, "",
                                state=None, smoke=False))
    assert seen["on_notify"] is None
    assert "the only developer on this run" in seen["team"]


# ── on-demand skills ─────────────────────────────────────────────────────────


def test_index_mode_lists_skills_instead_of_embedding_them():
    prompt = system_prompt_for(
        Role.WORKER, ui_product=True, extra_skills=("fastapi",), skill_mode="index",
        required_skills=("security",),
    )
    assert "security (required — already loaded below)" in prompt
    assert "fastapi (recommended)" in prompt and "ui_product (recommended)" in prompt
    # recommended packs stay on demand; the required one is embedded once
    assert load_skill("fastapi") not in prompt and load_skill("ui_product") not in prompt
    assert load_skill("security").strip() in prompt
    assert prompt.count(load_skill("security").strip()) == 1


def test_required_skills_are_embedded_so_loading_one_costs_nothing():
    """run-aa748f20's Dev A loaded the same two required packs seven times:
    each load a full turn, each lost again at compaction."""
    async def base(name, args):
        return "wrote it"

    async def scenario():
        wrapped, _, _ = wrap_coding_tools(
            base, gateway=None, settings=Settings(_env_file=None), agent_id="w",
            required_skills=("fastapi",),
        )
        return (
            await wrapped("skill", {"name": "fastapi"}),
            await wrapped("skill", {"name": "prod_ops"}),
            await wrapped("write_file", {"path": "a.py", "content": "x"}),
        )

    required, optional, write = asyncio.run(scenario())
    assert "already in your instructions" in required and load_skill("fastapi") not in required
    assert optional == load_skill("prod_ops")  # an optional pack still loads on demand
    assert write == "wrote it"  # no reminder noise


def test_the_agentic_worker_prompt_uses_the_index(monkeypatch):
    from kiln.agents import worker
    from kiln.schemas.tasks import Task

    seen = {}

    async def fake_loop(**kwargs):
        seen["system"] = kwargs["system"]
        seen["tools"] = [t["function"]["name"] for t in kwargs["tools"]]
        raise worker.AgentLoopError("stop here")

    monkeypatch.setattr(worker, "run_agent_loop", fake_loop)

    class GW:
        settings = Settings(_env_file=None)

    task = Task(id="t", title="T", description="d", owned_paths=["a.py"], required_skills=["fastapi"])

    async def call_tool(name, args):
        return ""

    with pytest.raises(worker.WorkerError):
        asyncio.run(worker.run_agentic(GW(), task, SPEC, call_tool, extra_skills=("fastapi",)))
    assert "fastapi (required" in seen["system"]
    assert seen["system"].count(load_skill("fastapi").strip()) == 1  # embedded once
    assert "notify_peer" not in seen["tools"]  # no team callback: tool withheld


# ── peer messages in the loop ────────────────────────────────────────────────


def test_peer_messages_arrive_between_turns_with_valid_pairing():
    calls = []
    # nothing before the first turn; the note arrives while the agent works
    queue = [["Dev B: use #note-list for the list element"], []]

    class GW:
        def complete(self, role, messages, **kwargs):
            calls.append([dict(m) for m in messages])
            if len(calls) == 1:
                return Completion(content="", model="m", prompt_tokens=1, completion_tokens=1,
                                  tool_calls=(ModelToolCall("c1", "read_file", '{"path": "a.py"}'),))
            return Completion(content="", model="m", prompt_tokens=1, completion_tokens=1,
                              tool_calls=(ModelToolCall("c2", "submit_result", json.dumps({
                                  "task_id": "t", "summary": "s", "files_written": ["a.py"],
                                  "what_works": "w", "known_gaps": []})),))

    async def call_tool(name, args):
        return "body"

    asyncio.run(run_agent_loop(
        gateway=GW(), role=Role.WORKER, system="s", user="u",
        tools=specs_for(("read_file", "submit_result")), call_tool=call_tool,
        finish=SubmitDev, max_steps=4, inbox=lambda: queue.pop() if queue else [],
    ))
    second = calls[1]
    roles = [m["role"] for m in second]
    assert roles[-3:] == ["assistant", "tool", "user"]  # the note follows the tool result
    assert "#note-list" in second[-1]["content"] and "<peer_messages>" in second[-1]["content"]


# ── shell verification ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "destructive"),
    [
        ("git reset --hard HEAD", True),
        ("git clean -fd", True),
        ("git checkout .", True),
        ("git checkout -- .", True),
        ("git restore .", True),
        ("git stash", True),
        ("git stash push -m wip", True),
        ("git stash list", False),
        ("git stash pop", False),
        ("git checkout main", False),
        ("git status", False),
    ],
)
def test_git_commands_that_wipe_work_are_destructive(command, destructive):
    assert scan_command(command).destructive is destructive


@pytest.mark.parametrize(
    ("command", "shell", "targets"),
    [
        ("echo hi > out.txt", "posix", ["out.txt"]),
        ("npm test >> log.txt 2>&1", "posix", ["log.txt"]),
        ("rm -rf build dist", "posix", ["build", "dist"]),
        ("mv a.ts b.ts", "posix", ["a.ts", "b.ts"]),
        ("cp -r src dst", "posix", ["dst"]),
        ("sed -i 's/a/b/' app.js", "posix", ["app.js"]),
        ("cat a | tee copy.txt", "posix", ["copy.txt"]),
        ("echo x > $HOME/x", "posix", []),  # variables are left to review
        ("rm *.log", "posix", []),
        ("Set-Content -Path notes.md -Value hi", "powershell", ["notes.md"]),
        ("Remove-Item -Recurse -Force .\\tmp", "powershell", [".\\tmp"]),
        ("npm run build > $null", "powershell", []),
        ("del /q old.txt", "cmd", ["old.txt"]),
    ],
)
def test_write_targets_are_read_literally(command, shell, targets):
    assert write_targets(command, shell) == targets


def _executor(tmp_path):
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())
    a = AgentScope(id="worker-1", label="Dev A", role="worker", owned_paths=["backend/"])
    b = AgentScope(id="worker-2", label="Dev B", role="worker", owned_paths=["frontend/"])
    executor.execute("claim_scope", {"owned_paths": ["backend/"]}, a)
    executor.execute("claim_scope", {"owned_paths": ["frontend/"]}, b)
    return executor, a, b


def test_a_shell_write_into_the_teammates_files_is_refused_before_it_runs(tmp_path, monkeypatch):
    executor, a, _ = _executor(tmp_path)
    ran = []
    monkeypatch.setattr("kiln.companion.executor.run_shell", lambda *x, **k: ran.append(x))
    with pytest.raises(PermissionDenied, match="leased to Dev B"):
        executor.execute("shell", {"command": "echo hi > frontend/App.tsx"}, a)
    with pytest.raises(PermissionDenied, match="outside the workspace"):
        executor.execute("shell", {"command": "echo hi > ../escape.txt"}, a)
    assert ran == []


def test_change_attribution_between_two_developers(tmp_path):
    executor, *_ = _executor(tmp_path)
    started = time.monotonic()
    before = {"frontend/App.tsx": (1, 1), "frontend/Page.tsx": (1, 1), "dist/x.js": (1, 1),
              "package.json": (1, 1)}
    after = {"frontend/App.tsx": (2, 2), "frontend/Page.tsx": (2, 2), "dist/x.js": (2, 2),
             "package.json": (2, 2)}
    executor._tool_writes["frontend/Page.tsx"] = ("worker-2", started + 0.01)  # B's own write
    leaked, manifests = executor._attribute_changes(
        before, after, "worker-1", started, started + 1, installs=True
    )
    assert leaked == ["frontend/App.tsx"]  # B's tool write and unowned output are not blamed
    assert manifests == ["package.json"]
    executor._shell_runs.append(("worker-2", started, started + 0.5))  # overlapping command
    leaked, _ = executor._attribute_changes(before, after, "worker-1", started, started + 1, installs=False)
    assert leaked == []  # ambiguous: B's own command may have changed its file


def test_the_change_check_skips_nested_installs(tmp_path):
    (tmp_path / "frontend" / "node_modules" / "react").mkdir(parents=True)
    (tmp_path / "frontend" / "node_modules" / "react" / "index.js").write_text("x")
    (tmp_path / "frontend" / "App.tsx").write_text("x")
    prints = _fingerprint(tmp_path)
    assert list(prints) == ["frontend/App.tsx"]


# ── self-healing suite ───────────────────────────────────────────────────────


def test_a_suite_that_cannot_run_is_healed_once(monkeypatch):
    class HealRegistry(FakeRegistry):
        def __init__(self):
            super().__init__([_report(passed=1, green=True)])
            self.broken = True

        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "run_tests" and self.broken:
                self.calls.append((tool, tool_input))
                raise RuntimeError("vitest is not installed at the workspace root")
            if tool == "install_deps":
                self.broken = False
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = HealRegistry()
    orch = _orch(TEST_SETTINGS, registry)
    report = asyncio.run(orch._run_suite("r"))
    assert report is not None and report.green
    assert any("reinstalling its toolchain fixed it" in n for n in orch._run_notes)


def test_a_suite_that_never_runs_ends_not_green_but_writes_the_docs(monkeypatch):
    from kiln.schemas.report import ReportBundle

    _agents(monkeypatch)
    seen = {}

    def fake_reporter(*_a, **kwargs):
        seen["notes"] = kwargs.get("run_notes")
        return ReportBundle(readme="r", run_summary="s", cost_report="c")

    monkeypatch.setattr(orchestrator.reporter, "run", fake_reporter)

    class NoSuite(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "run_tests":
                self.calls.append((tool, tool_input))
                raise RuntimeError("tests produced no kiln-results.xml")
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    registry = NoSuite([])
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("r", "a to-do app", [])) is False
    assert events.events[-1].kind == "run_finished"
    assert any("could not run" in note for note in seen["notes"])
    assert "README.md" in [inp.get("path") for tool, inp in registry.calls if tool == "write_file"]


def test_full_run_with_two_developers_goes_green(monkeypatch):
    _agents(monkeypatch)
    used = set()

    def worker_spy(gw, task, *a, **k):
        used.add(k.get("team_context", "")[:40])
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run", worker_spy)
    registry = FakeRegistry([_report(passed=1, failed=1), _report(passed=2, green=True)])
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("r", "a to-do app", [])) is True
    assert used and all(context.startswith("<team>") for context in used)
    assert {e.status for e in orch._board.tasks.values()} == {"done"}
    assert set(orch._board.tasks) == {task.id for task in GRAPH.tasks}
