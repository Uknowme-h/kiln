"""The tech lead supervisor and its floor events (docs/tech-lead-supervisor-plan.md)."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from kiln.agents import supervisor
from kiln.agents.coding_tools import wrap_coding_tools
from kiln.events.demo import script_demo_run
from kiln.events.schema import TeamEvent, TeamUpdate, decode_event, encode_event
from kiln.gateway.client import Completion
from kiln.graph import lead as lead_module
from kiln.graph import orchestrator
from kiln.graph.lanes import TeamBoard
from kiln.graph.orchestrator import Orchestrator
from kiln.graph.shell_hints import shell_hint
from kiln.schemas.supervisor import Decision, Escalation
from kiln.schemas.tasks import TaskGraph
from tests.test_orchestrator import Collector, FakeGateway, FakeRegistry, _work
from tests.test_two_developers import SPLIT, _agentic, _t

# ── shell hints ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        ("'vite' is not recognized as an internal or external command\n[exit code 1]", "missing_command"),
        ("bash: pnpm: command not found\n[exit code 127]", "missing_command"),
        ("Error: listen EADDRINUSE: address already in use :::3000\n[exit code 1]", "port_in_use"),
        ("EACCES: permission denied, open 'x'\n[exit code 1]", "permission"),
        ("[command exceeded 120s and was stopped; retry with a longer timeout]", "timeout"),
        ("Error: Cannot find module 'express'\n[exit code 1]", "missing_module"),
    ],
)
def test_common_shell_failures_get_a_hint(output, kind):
    found = shell_hint(output)
    assert found is not None and found[0] == kind and found[1]


def test_a_successful_command_gets_no_hint():
    assert shell_hint("grep found: command not found in docs") is None
    assert shell_hint("weird failure\n[exit code 2]") is None


# ── the decision schema and the model call ──────────────────────────────────


def test_a_decision_needs_the_payload_its_action_names():
    with pytest.raises(ValidationError, match="needs a question"):
        Decision(action="ask_user", message="m")
    with pytest.raises(ValidationError, match="needs an amendment"):
        Decision(action="amend_plan", message="m")
    with pytest.raises(ValidationError, match="recommended must be one of"):
        Decision.model_validate({"action": "ask_user", "message": "m",
                                 "question": {"question": "q", "options": ["a", "b"], "recommended": "c"}})
    assert Decision(action="answer", message="use /api/notes").action == "answer"


class _ScriptedGateway:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, role, messages, **kwargs):
        self.calls.append(messages)
        return Completion(content=self.replies.pop(0), model="m", prompt_tokens=1, completion_tokens=1)


def test_decide_retries_once_with_the_validation_error():
    good = json.dumps({"action": "answer", "message": "use port 5174"})
    gw = _ScriptedGateway('{"action": "ask_user", "message": "x"}', good)
    assert supervisor.decide(gw, "ctx").message == "use port 5174"
    assert "validation_error" in gw.calls[1][1]["content"]
    with pytest.raises(supervisor.SupervisorError):
        supervisor.decide(_ScriptedGateway("{}", "{}"), "ctx")


def test_the_context_carries_spec_board_and_escalation():
    text = supervisor.build_context(
        idea="notes app", spec=SPLIT, discovery="friendly dates", plan_memo="", board="<team_board>x</team_board>",
        escalation=Escalation(kind="product", summary="relative dates?", source="worker-2", task_id="ui"),
        decisions=["earlier call"],
    )
    for piece in ("<user_idea>", "c1: given g", "friendly dates", "<team_board>", "earlier call",
                  "kind: product", "from: worker-2", "relative dates?"):
        assert piece in text


# ── the orchestrator side ────────────────────────────────────────────────────


class _Registry(FakeRegistry):
    def __init__(self, answer=None, shell_output="ok", files=None):
        super().__init__([], files=files)
        self.answer = answer
        self.shell_output = shell_output

    async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
        if tool == "ask_user":
            self.calls.append((tool, tool_input))
            if self.answer is None:
                raise Exception("the user did not answer")
            return f'User answered: "{tool_input["questions"][0]["question"]}"="{self.answer}"'
        if tool == "shell":
            self.calls.append((tool, tool_input))
            return self.shell_output
        if tool == "write_file":
            self.files[tool_input["path"]] = tool_input["content"]
        return await super().call_tool(run_id, tool, tool_input, timeout, agent)


def _graph(*tasks):
    return TaskGraph.model_validate({"spec_title": "N", "tasks": list(tasks)})


def _lead_orch(graph=None, registry=None, events=None, **settings):
    orch = Orchestrator(FakeGateway(), registry or _Registry(), (events or Collector()).send, 1,
                        _agentic(**settings))
    orch._spec = SPLIT
    orch._run_id = "r"
    orch._idea = "notes"
    if graph is not None:
        orch._graph = graph
        orch._team(graph)
    return orch


def _decide_with(monkeypatch, *decisions):
    queue = [Decision.model_validate(d) for d in decisions]
    seen = []

    def fake(gw, context):
        seen.append(context)
        return queue.pop(0)

    monkeypatch.setattr(lead_module.supervisor, "decide", fake)
    return seen


def test_an_answer_reaches_the_developer_and_the_floor(monkeypatch):
    events = Collector()
    orch = _lead_orch(_graph(_t("a", ["a.py"]), _t("b", ["b.py"])), events=events)
    seen = _decide_with(monkeypatch, {"action": "answer", "message": "use /api/notes"})
    reply = asyncio.run(orch._escalate(
        Escalation(kind="blocked", summary="which route?", source="worker-1"), wait=True, reply_lane="A"))
    assert reply == "Tech lead (answer): use /api/notes"
    assert "which route?" in seen[0]
    team = [e for e in events.events if isinstance(e, TeamEvent)]
    assert [e.category for e in team] == ["escalation", "decision"]
    assert team[0].from_agent == "worker-1" and team[0].to_agent == "supervisor"
    assert orch._board.decisions == 1
    assert any(isinstance(e, TeamUpdate) and e.decisions == 1 for e in events.events)
    assert any("tech lead" in note for note in orch._run_notes)


def test_the_lead_is_capped_off_or_failing_without_breaking_anything(monkeypatch):
    calls = _decide_with(monkeypatch)  # any call would pop an empty queue
    capped = _lead_orch(supervisor_max_calls=0)
    off = _lead_orch(enable_supervisor=False)
    esc = Escalation(kind="plan", summary="too big")
    assert "budget" in asyncio.run(capped._escalate(esc, wait=True, reply_lane=None))
    assert "turned off" in asyncio.run(off._escalate(esc, wait=True, reply_lane=None))
    assert calls == []

    def boom(gw, context):
        raise RuntimeError("model down")

    monkeypatch.setattr(lead_module.supervisor, "decide", boom)
    reply = asyncio.run(_lead_orch()._escalate(esc, wait=True, reply_lane=None))
    assert "could not decide" in reply and "known_gaps" in reply


def test_decisions_are_serialized(monkeypatch):
    active, peak = [0], [0]

    def slow(gw, context):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        import time

        time.sleep(0.05)
        active[0] -= 1
        return Decision(action="answer", message="ok")

    monkeypatch.setattr(lead_module.supervisor, "decide", slow)
    orch = _lead_orch()

    async def both():
        esc = Escalation(kind="blocked", summary="x")
        return await asyncio.gather(orch._escalate(esc, wait=True, reply_lane=None),
                                    orch._escalate(esc, wait=True, reply_lane=None))

    assert len(asyncio.run(both())) == 2 and peak[0] == 1


QUESTION = {"action": "ask_user", "message": "go with it",
            "question": {"question": "Relative or absolute dates?", "options": ["relative", "absolute"],
                         "recommended": "relative"}}


def test_a_user_answer_is_used_and_recorded_in_discovery(monkeypatch):
    registry = _Registry(answer="absolute", files={"docs/discovery.md": "# Discovery\n- friendly UI\n"})
    orch = _lead_orch(_graph(_t("a", ["a.py"]), _t("b", ["b.py"])), registry=registry)
    _decide_with(monkeypatch, QUESTION)
    reply = asyncio.run(orch._escalate(Escalation(kind="product", summary="dates"), wait=True, reply_lane="A"))
    assert "the user decided: absolute" in reply
    doc = registry.files["docs/discovery.md"]
    assert doc.startswith("# Discovery") and "## Decisions during the build" in doc
    assert "Relative or absolute dates? → absolute (user)" in doc
    # both developers hear about it
    assert all(any("product decision" in n for n in [*lane.inbox, *orch._board.notes])
               for lane in orch._board.lanes.values())


def test_answers_with_quotes_are_parsed_by_the_exact_question():
    q = 'Label the button "Save" or "Done"?'
    assert lead_module._parse_answer(f'User answered: "{q}"="say \\"Done\\""', q) == 'say \\"Done\\"'
    assert lead_module._parse_answer(f'User answered: "{q}"="(skipped)"', q) == "(skipped)"
    assert lead_module._parse_answer("garbage", q) == ""


def test_silence_takes_the_recommended_option(monkeypatch):
    registry = _Registry(answer=None)
    orch = _lead_orch(registry=registry)
    _decide_with(monkeypatch, QUESTION)
    reply = asyncio.run(orch._escalate(Escalation(kind="product", summary="dates"), wait=True, reply_lane=None))
    assert "taking the recommended option: relative" in reply
    asked = next(inp for tool, inp in registry.calls if tool == "ask_user")
    assert asked["timeout_seconds"] == orch.settings.supervisor_user_timeout_seconds
    assert asked["questions"][0]["header"] == "Tech lead"
    assert "relative (tech lead default (no answer))" in registry.files["docs/discovery.md"]


def test_an_install_outside_the_stack_is_refused(monkeypatch):
    registry = _Registry()
    orch = _lead_orch(registry=registry)
    _decide_with(monkeypatch, {"action": "install", "message": "installing", "install": "rustc"})
    reply = asyncio.run(orch._escalate(Escalation(kind="environment", summary="no rust"),
                                       wait=True, reply_lane=None))
    assert "does not use it" in reply
    assert not any(tool == "install_tool" for tool, _ in registry.calls)


def test_a_non_blocking_escalation_arrives_as_a_message(monkeypatch):
    orch = _lead_orch(_graph(_t("a", ["a.py"]), _t("b", ["b.py"])))
    _decide_with(monkeypatch, {"action": "proceed", "message": "stub it and record a gap"})

    async def go():
        orch._board.start("a", "A")
        reply = await orch._escalate(Escalation(kind="blocked", summary="x"), wait=False, reply_lane="A")
        await asyncio.gather(*orch._supervisor_jobs)
        await asyncio.sleep(0)
        return reply

    assert "keep working" in asyncio.run(go())
    assert any("stub it and record a gap" in line for line in orch._board.lanes["A"].inbox)


# ── plan amendments ──────────────────────────────────────────────────────────


def _three():
    return _graph(_t("s", ["app/main.py"], kind="scaffold"), _t("big", ["app/a.py", "app/b.py"], ["s"]),
                  _t("ui", ["app/ui.py"], ["big"]))


def _amend(orch, amendment):
    from kiln.schemas.supervisor import PlanAmendment

    orch._building = True
    return asyncio.run(orch._apply_amendment(PlanAmendment.model_validate(amendment)))


def test_a_pending_task_is_split_and_its_dependents_follow():
    orch = _lead_orch(_three())
    ok, info = _amend(orch, {"replace_task": "big", "add_tasks": [
        _t("big1", ["app/a.py"], ["big"]), _t("big2", ["app/b.py"], ["big1"])]})
    assert ok, info
    assert "big split into big1, big2" in info
    by_id = {t.id: t for t in orch._graph.tasks}
    assert "big" not in by_id and "big" not in orch._board.tasks
    assert by_id["big1"].depends_on == ["s"]  # inherited the replaced task's dependencies
    assert set(by_id["ui"].depends_on) == {"big1", "big2"}
    assert orch._board.tasks["ui"].task.depends_on == by_id["ui"].depends_on
    assert {"big1", "big2"} <= orch._added_task_ids
    assert any("PLAN CHANGED" in n for n in orch._board.notes)


@pytest.mark.parametrize(
    ("amendment", "reason"),
    [
        ({"add_tasks": [_t("x", ["app/a.py"])]}, "invalid"),  # overlaps big's lease
        ({"dependencies": [{"task_id": "s", "depends_on": ["ui"]}]}, "started"),
        ({"dependencies": [{"task_id": "big", "depends_on": ["ui"]}]}, "invalid"),  # a cycle
        ({"replace_task": "s", "add_tasks": [_t("s2", ["app/main.py"])]}, "running or done"),
        ({"add_tasks": [_t("ui", ["app/z.py"])]}, "already in use"),
        ({"reassign": [{"task_id": "s", "developer": "B"}]}, "cannot change hands"),
    ],
)
def test_invalid_amendments_are_refused_and_change_nothing(amendment, reason):
    orch = _lead_orch(_three())
    orch._board.start("s", "A")
    before = [t.model_dump() for t in orch._graph.tasks]
    ok, info = _amend(orch, amendment)
    assert not ok and reason in info
    assert [t.model_dump() for t in orch._graph.tasks] == before


def test_no_amendment_after_the_build_stage():
    from kiln.schemas.supervisor import PlanAmendment

    orch = _lead_orch(_three())
    ok, info = asyncio.run(orch._apply_amendment(PlanAmendment(add_tasks=[])))
    assert not ok and "build stage is over" in info


def test_a_failed_task_replaced_by_the_lead_is_built(monkeypatch):
    """The end-to-end self-heal: a task fails twice (retry + failover), the
    lead replaces it, and the scheduler builds the replacement."""
    graph = _graph(_t("s", ["app/main.py"], kind="scaffold"), _t("bad", ["app/bad.py"], ["s"]))
    orch = _lead_orch(graph, worker_retries=0)
    _decide_with(monkeypatch, {"action": "amend_plan", "message": "smaller piece", "amendment": {
        "replace_task": "bad", "add_tasks": [_t("good", ["app/bad.py"], ["s"])]}})
    built = []

    async def fake(gw, task, spec, call_tool, **kwargs):
        built.append(task.id)
        if task.id == "bad":
            raise RuntimeError("cannot finish")
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    from kiln.schemas.suite import TestSuite

    suite = TestSuite.model_validate({"spec_title": "N", "files": [
        {"path": "tests/test_a.py", "content": "x", "covers": ["c1"]}]})
    orch._suite = suite

    async def go():
        orch._building = True
        state = orchestrator._SuiteState()
        await orch._run_tasks("r", list(graph.tasks), SPLIT, suite, {}, graph, "", state=state, smoke=False)

    monkeypatch.setattr(Orchestrator, "_refresh_suite_state", lambda self, run_id, state: asyncio.sleep(0))
    asyncio.run(go())
    assert built.count("bad") == 2 and "good" in built
    assert orch._board.tasks["good"].status == "done"
    assert "bad" not in orch._failed_tasks


# ── the developer's side: tool wiring and the shell wrapper ─────────────────


def test_escalate_without_a_lead_tells_the_developer_to_decide():
    async def companion(name, args):
        raise AssertionError("escalate must not reach the companion")

    call, _, _ = wrap_coding_tools(companion, gateway=FakeGateway(), settings=_agentic(), agent_id="w")
    assert "no tech lead" in asyncio.run(call("escalate", {"kind": "product", "summary": "x"}))

    async def lead(payload):
        return f"lead saw {payload.kind}: {payload.summary}"

    call, _, _ = wrap_coding_tools(companion, gateway=FakeGateway(), settings=_agentic(), agent_id="w",
                                   on_escalate=lead)
    assert asyncio.run(call("escalate", {"kind": "plan", "summary": "too big"})) == "lead saw plan: too big"
    assert asyncio.run(call("escalate", {"kind": "nope", "summary": "x"})).startswith("Error:")


def test_a_recurring_shell_failure_escalates_once(monkeypatch):
    graph = _graph(_t("a", ["a.py"]))
    registry = _Registry(shell_output="'vite' is not recognized as an internal or external command\n[exit code 1]")
    orch = _lead_orch(graph, registry=registry)
    seen = _decide_with(monkeypatch, {"action": "answer", "message": "use npx --no-install vite"})
    outputs = []

    async def fake(gw, task, spec, call_tool, **kwargs):
        for _ in range(3):
            outputs.append(await call_tool("shell", {"command": "vite build"}))
        await asyncio.gather(*orch._supervisor_jobs)
        return _work(task.id, task.owned_paths[0])

    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    from kiln.schemas.suite import TestSuite

    suite = TestSuite.model_validate({"spec_title": "N", "files": [
        {"path": "tests/test_a.py", "content": "x", "covers": ["c1"]}]})
    orch._suite = suite
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, suite, {}, graph, "", state=None, smoke=False))
    assert all("[hint: the command is not on PATH" in out for out in outputs)
    assert "the tech lead has been told" in outputs[1] and "tech lead" not in outputs[2]
    assert len(seen) == 1 and "recurring shell failure (missing_command)" in seen[0]


# ── floor events ─────────────────────────────────────────────────────────────


def test_the_board_reports_messages_by_category_and_snapshots_validate():
    graph = _graph(_t("a", ["a.py"]), _t("b", ["b.py"]))
    board = TeamBoard(graph, SPLIT)
    heard = []
    board.listener = lambda *msg: heard.append(msg)
    board.start("a", "A")
    qid = board.ask("B", "A", "lowercase emails?")
    board.answer("A", qid, "yes")
    board.post(None, "A", "do this", category="decision", sender_label="Lena Lead")
    board.finish("a", "A", summary="done", files=["a.py"], gaps=[])
    cats = [c for c, *_ in heard]
    assert cats[:3] == ["question", "answer", "decision"] and "status" in cats
    assert heard[0][1:3] == ("worker-2", "worker-1")
    assert heard[2][1] == "supervisor"
    snap = TeamUpdate(**board.snapshot())
    assert {lane.agent_id for lane in snap.lanes} == {"worker-1", "worker-2"}
    assert {t.id: t.status for t in snap.tasks} == {"a": "done", "b": "pending"}
    assert decode_event(encode_event(snap)).kind == "team_update"


def test_a_broken_listener_never_breaks_the_board():
    board = TeamBoard(_graph(_t("a", ["a.py"]), _t("b", ["b.py"])), SPLIT)

    def bad(*_):
        raise RuntimeError("floor down")

    board.listener = bad
    assert board.post("A", "B", "hi").endswith("next task")


def test_the_overview_shows_every_task_and_both_developers():
    board = TeamBoard(_three(), SPLIT)
    text = board.render_overview()
    assert "Dev A" in text and "Dev B" in text
    for tid in ("s", "big", "ui"):
        assert f"- {tid} [" in text
    assert "after big" in text


def test_the_demo_shows_the_team_talking_and_the_lead_deciding():
    events = list(script_demo_run())
    cats = {e.category for e in events if e.kind == "team_event"}
    assert {"question", "answer", "contract", "escalation", "decision"} <= cats
    assert any(e.kind == "team_update" for e in events)
    assert events[-1].notes
