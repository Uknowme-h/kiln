"""Regressions from run-aa748f20 (both developers idle after their first task,
Dev A's tokens spent on re-reads and skill reloads)."""

import asyncio
import json
from pathlib import Path

import pytest

from kiln.agents.loop import AgentLoopError, run_agent_loop
from kiln.agents.roles import Role
from kiln.companion.offload import maybe_offload_tool_output
from kiln.gateway.client import Completion, ModelToolCall
from kiln.graph import orchestrator
from kiln.graph.orchestrator import Orchestrator, _SuiteState
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import TaskGraph
from kiln.stacks.profiles import profile_for
from kiln.tools.registry import specs_for
from kiln.tools.schemas import SubmitDev
from tests.test_orchestrator import Collector, FakeGateway, FakeRegistry, _report, _work
from tests.test_two_developers import SPLIT, _agentic, _t

# ── 1. machine-readable results are never offloaded ──────────────────────────


@pytest.mark.parametrize(
    "tool", ["run_tests", "served_ui_audit", "runtime_smoke", "ensure_browser_runtime", "browser_verify"]
)
def test_results_kiln_parses_are_never_offloaded(tmp_path: Path, tool):
    report = json.dumps({"cases": [{"nodeid": f"t{i}", "message": "x" * 200} for i in range(200)]})
    assert maybe_offload_tool_output(tmp_path, tool=tool, output=report, threshold=8_000) == report


def test_an_offloaded_report_is_named_not_a_json_error():
    class Stubbing(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "run_tests":
                return "[tool output offloaded — 28340 chars]\npath: x"
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    orch = Orchestrator(FakeGateway(), Stubbing([]), Collector().send, 1, _agentic())
    with pytest.raises(orchestrator.LoopError, match="restart the companion"):
        asyncio.run(orch.run_tests("r"))


# ── 2. one python environment, where the suite runs ──────────────────────────


def test_a_python_root_installs_into_the_workspace_venv(tmp_path: Path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text("fastapi\n")
    commands = profile_for("python").install_deps_commands(
        backend, "Windows", ["py", "-3"], venv_root=tmp_path
    )
    assert commands[0] == ["py", "-3", "-m", "venv", str(tmp_path / ".venv")]
    pip = commands[1]
    assert pip[0] == str(tmp_path / ".venv" / "Scripts" / "python.exe")  # not backend/.venv
    assert pip[-2:] == ["-r", "requirements.txt"]  # run with backend/ as cwd


# ── 3. no barrier between one root's scaffold and the next task on it ───────


class _Timeline(FakeRegistry):
    def __init__(self, timeline, reports):
        super().__init__(reports)
        self.timeline = timeline

    async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
        if tool == "install_deps":
            self.timeline.append(("install", tool_input.get("root", ".")))
        if tool == "run_tests":
            self.timeline.append(("suite", ""))
        return await super().call_tool(run_id, tool, tool_input, timeout, agent)


def _split_graph():
    return TaskGraph.model_validate({"spec_title": "N", "tasks": [
        _t("be", ["backend/app.js"], root="backend", kind="scaffold"),
        _t("fe", ["frontend/src/App.tsx"], root="frontend", kind="scaffold"),
        _t("api", ["backend/routes.js"], ["be"], root="backend"),
        _t("ui", ["frontend/src/Page.tsx"], ["fe"], root="frontend"),
    ]})


def test_the_frontend_builds_on_while_the_backend_scaffold_is_still_running(monkeypatch):
    timeline = []
    graph = _split_graph()
    registry = _Timeline(timeline, [_report(passed=1, failed=1)] * 4)
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, _agentic())
    orch.stack_id = "node"
    orch._spec = SPLIT
    suite = TestSuite.model_validate({"spec_title": "N", "files": [
        {"path": "tests/a.test.ts", "content": "x", "covers": ["c1"]}]})

    async def fake(gw, task, spec, call_tool, **kwargs):
        timeline.append(("start", task.id))
        await asyncio.sleep(0.3 if task.id == "be" else 0.01)
        timeline.append(("done", task.id))
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, suite, {}, graph, "",
                                state=_SuiteState(), smoke=True, prepare=True))
    at = timeline.index
    # Dev B's next task started while Dev A was still on the backend scaffold
    assert at(("start", "ui")) < at(("done", "be"))
    # ...but only after its own root's dependencies were installed
    assert at(("install", "frontend")) < at(("start", "ui"))
    assert at(("install", "backend")) < at(("start", "api"))
    # each root installs once; the node runner at the root once every scaffold settled
    installs = [root for kind, root in timeline if kind == "install"]
    assert sorted(installs) == [".", "backend", "frontend"]
    assert at(("install", ".")) > at(("done", "be"))
    assert all(orch._board.tasks[t].status == "done" for t in ("be", "fe", "api", "ui"))


def test_a_failed_root_install_never_strands_its_tasks(monkeypatch):
    class FailingInstall(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "install_deps":
                raise RuntimeError("npm ERR! network")
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    graph = _split_graph()
    orch = Orchestrator(FakeGateway(), FailingInstall([_report(passed=0, failed=1)] * 4),
                        Collector().send, 1, _agentic())
    orch._spec = SPLIT
    built = []

    async def fake(gw, task, spec, call_tool, **kwargs):
        built.append(task.id)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    suite = TestSuite.model_validate({"spec_title": "N", "files": [
        {"path": "tests/a.test.ts", "content": "x", "covers": ["c1"]}]})
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, suite, {}, graph, "",
                                state=_SuiteState(), smoke=True, prepare=True))
    assert sorted(built) == ["api", "be", "fe", "ui"]
    assert any("npm ERR! network" in note for note in orch._run_notes)


# ── 4. a companion outage pauses the team instead of feeding it errors ──────


class _Outage(FakeRegistry):
    def __init__(self, failures, error):
        super().__init__([])
        self.failures = failures
        self.error = error
        self.attempts = 0

    async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.error
        return await super().call_tool(run_id, tool, tool_input, timeout, agent)


def test_a_call_waits_out_a_companion_outage_and_the_floor_is_told():
    from kiln.companion.registry import NO_COMPANION, CompanionUnavailable

    events = Collector()
    registry = _Outage(2, CompanionUnavailable(NO_COMPANION))
    orch = Orchestrator(FakeGateway(), registry, events.send, 1, _agentic())
    assert asyncio.run(orch.call_tool("r", "grep", {"pattern": "x"})) == "ok"
    assert registry.attempts == 3
    told = [e.summary for e in events.events if e.kind == "message_sent"]
    assert any("paused until it reconnects" in s for s in told)
    assert any("companion is back" in s for s in told)
    assert told.count(next(s for s in told if "paused" in s)) == 1  # said once per outage


def test_a_call_cut_off_mid_flight_is_never_repeated():
    from kiln.companion.registry import CompanionUnavailable

    registry = _Outage(1, CompanionUnavailable("companion disconnected"))
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, _agentic())
    with pytest.raises(CompanionUnavailable):
        asyncio.run(orch.call_tool("r", "write_file", {"path": "a", "content": "x"}))
    assert registry.attempts == 1


def test_an_outage_past_the_limit_fails_fast():
    from kiln.companion.registry import NO_COMPANION, CompanionUnavailable

    registry = _Outage(99, CompanionUnavailable(NO_COMPANION))
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1,
                        _agentic(companion_outage_wait_seconds=0))
    with pytest.raises(CompanionUnavailable):
        asyncio.run(orch.call_tool("r", "grep", {"pattern": "x"}))
    assert registry.attempts == 1


# ── 5. re-reads: answered from context, legitimate after compaction ─────────


def _turn(name, args, cid):
    return Completion(content="", tool_calls=[ModelToolCall(id=cid, name=name, arguments=json.dumps(args))],
                      model="m", prompt_tokens=1, completion_tokens=1)


SUBMIT = {"task_id": "t1", "summary": "s", "files_written": ["a.py"], "what_works": "w", "known_gaps": []}


def _drive(script, call_tool, **kwargs):
    seen = []

    class GW:
        def complete(self, role, messages, **kw):
            seen.append((kw.get("agent_id"), [dict(m) for m in messages]))
            if str(kw.get("agent_id", "")).endswith("/compact"):
                return Completion(content="## Objective\nx", model="m", prompt_tokens=1, completion_tokens=1)
            return script.pop(0)

    outcome = asyncio.run(run_agent_loop(
        gateway=GW(), role=Role.WORKER, system="sys", user=kwargs.pop("user", "brief"),
        tools=specs_for(("read_file", "write_file", "skill", "submit_result")), call_tool=call_tool,
        finish=SubmitDev, max_steps=kwargs.pop("max_steps", 12), **kwargs,
    ))
    return outcome, seen


def test_a_write_makes_the_next_read_real_again():
    executed = []

    async def call_tool(name, args):
        executed.append((name, args.get("path")))
        return "body" if name == "read_file" else "ok"

    script = [
        _turn("read_file", {"path": "a.py"}, "1"),
        _turn("read_file", {"path": "a.py"}, "2"),  # still visible: answered from context
        _turn("write_file", {"path": "a.py", "content": "x"}, "3"),
        _turn("read_file", {"path": "a.py"}, "4"),  # changed by the agent: read again
        _turn("submit_result", SUBMIT, "5"),
    ]
    _drive(script, call_tool)
    assert executed == [("read_file", "a.py"), ("write_file", "a.py"), ("read_file", "a.py")]


def test_rereading_after_compaction_is_not_a_loop():
    """run-aa748f20: compaction dropped the file contents, the developer read
    them again, and the repeat guard killed the attempt as "stuck"."""
    executed = []

    async def call_tool(name, args):
        executed.append(args.get("path"))
        return "y" * 500

    reads = [_turn("read_file", {"path": f"f{i % 2}.py"}, str(i)) for i in range(8)]
    outcome, seen = _drive(reads + [_turn("submit_result", SUBMIT, "s")], call_tool,
                           compact_threshold_chars=1_500, max_steps=12)
    assert outcome.result.task_id == "t1"
    assert any(str(aid).endswith("/compact") for aid, _ in seen)
    assert executed.count("f0.py") >= 2  # read again once the compaction removed it


def test_a_big_brief_alone_does_not_compact():
    async def call_tool(name, args):
        return "small"

    script = [_turn("read_file", {"path": "a.py"}, "1"), _turn("submit_result", SUBMIT, "2")]
    _, seen = _drive(script, call_tool, user="B" * 50_000, compact_threshold_chars=20_000)
    assert not any(str(aid).endswith("/compact") for aid, _ in seen)


def test_a_teammate_message_makes_the_next_read_real():
    executed = []
    inbox_notes = [[], ["Dev B: changed a.py (the form posts display_name now)"], [], []]

    async def call_tool(name, args):
        executed.append(args.get("path"))
        return "body"

    script = [
        _turn("read_file", {"path": "a.py"}, "1"),
        _turn("read_file", {"path": "a.py"}, "2"),  # the note arrived first: read again
        _turn("read_file", {"path": "a.py"}, "3"),  # nothing new since: answered from context
        _turn("submit_result", SUBMIT, "4"),
    ]
    _drive(script, call_tool, inbox=lambda: inbox_notes.pop(0) if inbox_notes else [])
    assert executed == ["a.py", "a.py"]


# ── 6. spec oracles point at files the suite wrote ──────────────────────────


def _oracle_spec(oracle, cid="c1"):
    from tests.test_two_developers import _spec

    spec = _spec()
    criterion = spec.acceptance_criteria[0].model_copy(update={"id": cid, "oracle": oracle})
    return spec.model_copy(update={"acceptance_criteria": [criterion]})


def _suite(*files):
    return TestSuite.model_validate({"spec_title": "N", "files": [
        {"path": path, "content": "x", "covers": covers} for path, covers in files]})


def test_an_oracle_naming_a_missing_file_points_at_the_covering_file():
    from kiln.agents.test_author import align_oracles

    spec = _oracle_spec("tests/test_health.py — GET /api/health returns 200 {status: ok}")
    fixed, notes = align_oracles(spec, _suite(("tests/test_embed_health.py", ["c1"]),
                                              ("tests/test_auth.py", ["c2"])))
    assert fixed.acceptance_criteria[0].oracle.startswith("tests/test_embed_health.py — GET")
    assert notes == ["c1: oracle tests/test_health.py → tests/test_embed_health.py (the file the suite wrote)"]
    assert spec.acceptance_criteria[0].oracle.startswith("tests/test_health.py")  # input untouched


def test_oracles_that_match_or_are_ambiguous_stay_as_written():
    from kiln.agents.test_author import align_oracles

    ok = _oracle_spec("tests/test_auth.py — signup returns 201")
    assert align_oracles(ok, _suite(("tests/test_auth.py", ["c1"]))) == (ok, [])
    vague = _oracle_spec("an automated check that signup works")
    assert align_oracles(vague, _suite(("tests/test_auth.py", ["c1"]))) == (vague, [])
    two = _oracle_spec("tests/test_x.py — y")
    fixed, notes = align_oracles(two, _suite(("tests/a.test.ts", ["c1"]), ("tests/b.test.ts", ["c1"])))
    assert fixed.acceptance_criteria[0].oracle == "tests/test_x.py — y"
    assert "which the suite lacks (covered by tests/a.test.ts, tests/b.test.ts)" in notes[0]


def test_refused_repeats_are_on_the_transcript():
    from kiln.agents.transcript import TranscriptLog

    log = TranscriptLog()

    async def call_tool(name, args):
        return "body"

    same = [_turn("read_file", {"path": "a.py"}, str(i)) for i in range(5)]
    with pytest.raises(AgentLoopError):
        _drive(same, call_tool, transcript=log)
    notes = [e for e in log.for_agent("worker") if e.kind == "note" and "refused" in str(e.payload.get("text"))]
    assert notes and notes[0].payload["name"] == "read_file"
