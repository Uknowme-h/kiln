"""The agent loop with a scripted model: tool turns, the cap handoff, and the guards."""

import asyncio
import json

import pytest

from kiln.agents.loop import AgentLoopError, run_agent_loop
from kiln.agents.roles import Role
from kiln.config import Settings
from kiln.gateway.budget import BudgetGuard
from kiln.gateway.client import Completion, Gateway, ModelToolCall
from kiln.gateway.errors import GatewayError
from kiln.gateway.usage import UsageLog
from kiln.tools.registry import TOOLS, specs_for
from kiln.tools.schemas import SubmitDev

TOOL_LIST = specs_for(("read_file", "shell", "submit_result"))


def call(name, args, call_id=None):
    return ModelToolCall(id=call_id or f"c-{name}", name=name, arguments=json.dumps(args))


def turn(*calls, text=""):
    return Completion(
        content=text, model="m", prompt_tokens=1, completion_tokens=1, tool_calls=tuple(calls)
    )


SUBMIT = {
    "task_id": "t1",
    "summary": "built it",
    "files_written": ["app.py"],
    "what_works": "all of it",
    "known_gaps": [],
}


class ScriptedGateway:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def complete(self, role, messages, **kwargs):
        self.seen.append({"messages": list(messages), **kwargs})
        return self.script.pop(0)


def drive(gateway, tools_called=None, max_steps=6, **kwargs):
    log = [] if tools_called is None else tools_called

    async def call_tool(name, args):
        log.append((name, args))
        if name == "shell" and args.get("command") == "boom":
            raise RuntimeError("PermissionDenied: user refused: use pytest instead")
        return "ok"

    return asyncio.run(
        run_agent_loop(
            gateway=gateway,
            role=Role.WORKER,
            system="sys",
            user="go",
            tools=TOOL_LIST,
            call_tool=call_tool,
            finish=SubmitDev,
            max_steps=max_steps,
            **kwargs,
        )
    )


def test_a_tool_turn_then_submit_finishes_with_a_validated_result():
    gateway = ScriptedGateway(
        [turn(call("read_file", {"path": "a.py"})), turn(call("submit_result", SUBMIT))]
    )
    log = []
    outcome = drive(gateway, log)
    assert outcome.result.task_id == "t1"
    assert outcome.steps == 2 and outcome.tool_calls == 1
    assert log == [("read_file", {"path": "a.py"})]
    # the tool result went back to the model as a tool message with the call id
    second = gateway.seen[1]["messages"]
    assert second[-1] == {"role": "tool", "tool_call_id": "c-read_file", "content": "ok"}
    assert second[-2]["tool_calls"][0]["function"]["name"] == "read_file"


def test_null_arguments_are_dropped_before_the_call():
    gateway = ScriptedGateway(
        [
            turn(call("read_file", {"path": "a.py", "offset": None, "limit": None})),
            turn(call("submit_result", SUBMIT)),
        ]
    )
    log = []
    drive(gateway, log)
    assert log == [("read_file", {"path": "a.py"})]


def test_a_refusal_goes_back_to_the_model_as_tool_output():
    gateway = ScriptedGateway(
        [turn(call("shell", {"command": "boom"})), turn(call("submit_result", SUBMIT))]
    )
    drive(gateway)
    refused = gateway.seen[1]["messages"][-1]["content"]
    assert "user refused: use pytest instead" in refused


def test_an_invalid_submit_is_rejected_and_the_agent_can_retry():
    bad = {**SUBMIT, "summary": ""}
    gateway = ScriptedGateway([turn(call("submit_result", bad)), turn(call("submit_result", SUBMIT))])
    outcome = drive(gateway)
    assert outcome.result.summary == "built it"
    assert "rejected" in gateway.seen[1]["messages"][-1]["content"]


def test_the_check_hook_can_reject_a_wrong_task_id():
    wrong = {**SUBMIT, "task_id": "t9"}
    gateway = ScriptedGateway([turn(call("submit_result", wrong)), turn(call("submit_result", SUBMIT))])
    outcome = drive(
        gateway, check=lambda p: None if p.task_id == "t1" else f"wrong task {p.task_id}"
    )
    assert outcome.result.task_id == "t1"
    assert "wrong task t9" in gateway.seen[1]["messages"][-1]["content"]


def test_malformed_arguments_are_reported_not_fatal():
    bad = ModelToolCall(id="c-x", name="read_file", arguments="{not json")
    gateway = ScriptedGateway([turn(bad), turn(call("submit_result", SUBMIT))])
    drive(gateway)
    assert "not valid JSON" in gateway.seen[1]["messages"][-1]["content"]


def test_unknown_tools_are_refused_with_the_available_list():
    gateway = ScriptedGateway([turn(call("rm_rf", {})), turn(call("submit_result", SUBMIT))])
    drive(gateway)
    assert "Unknown tool" in gateway.seen[1]["messages"][-1]["content"]


def test_the_cap_withdraws_tools_and_demands_a_handoff():
    reads = [turn(call("read_file", {"path": f"f{i}.py"})) for i in range(3)]
    gateway = ScriptedGateway(reads + [turn(call("submit_result", SUBMIT))])
    outcome = drive(gateway, max_steps=3)
    assert outcome.result.what_works == "all of it"
    last = gateway.seen[-1]
    assert [t["function"]["name"] for t in last["tools"]] == ["submit_result"]
    assert "out of steps" in last["messages"][-1]["content"]


def test_the_cap_without_a_valid_handoff_is_an_error():
    from kiln.agents.loop import HANDOFF_ATTEMPTS

    reads = [turn(call("read_file", {"path": f"f{i}.py"})) for i in range(2)]
    # every handoff attempt comes back without a submit
    gateway = ScriptedGateway(reads + [turn() for _ in range(HANDOFF_ATTEMPTS)])
    with pytest.raises(AgentLoopError, match="step cap"):
        drive(gateway, max_steps=2)


def test_the_third_identical_call_is_refused_and_repeated_strikes_end_the_run():
    same = call("read_file", {"path": "a.py"})
    gateway = ScriptedGateway([turn(same) for _ in range(5)])
    log = []
    with pytest.raises(AgentLoopError, match="stuck"):
        drive(gateway, log, max_steps=10)
    assert len(log) == 2  # the third and later identical calls never reach the tool
    assert "Refused" in gateway.seen[3]["messages"][-1]["content"]


def test_the_reminder_is_reattached_at_the_cap_and_on_doom_refusals():
    # step-cap handoff carries the contract reminder
    reads = [turn(call("read_file", {"path": f"f{i}.py"})) for i in range(3)]
    gateway = ScriptedGateway(reads + [turn(call("submit_result", SUBMIT))])
    outcome = drive(gateway, max_steps=3, reminder="the contract: routes + modules")
    assert outcome.result.task_id == "t1"
    handoff = gateway.seen[-1]["messages"][-1]["content"]
    assert "out of steps" in handoff and "the contract" in handoff

    # a doom-loop refusal re-asserts it too: the 3rd identical call is refused,
    # and that refusal (with the reminder) is in the conversation the model sees next
    same = call("read_file", {"path": "a.py"})
    turns = [turn(same) for _ in range(3)] + [turn(call("submit_result", SUBMIT))]
    gateway2 = ScriptedGateway(turns)
    outcome2 = drive(gateway2, reminder="own only app/")
    tool_outputs = [m["content"] for m in gateway2.seen[-1]["messages"] if m["role"] == "tool"]
    assert any("Refused" in text and "own only app/" in text for text in tool_outputs)
    assert outcome2.result.task_id == "t1"


def test_an_agent_that_only_talks_is_nudged_then_stopped():
    gateway = ScriptedGateway([turn(text="thinking out loud") for _ in range(3)])
    with pytest.raises(AgentLoopError, match="without submitting"):
        drive(gateway)
    nudge = gateway.seen[1]["messages"][-1]
    assert nudge["role"] == "user" and "submit_result" in nudge["content"]


def test_the_finish_tool_must_be_offered():
    with pytest.raises(ValueError):
        asyncio.run(
            run_agent_loop(
                gateway=ScriptedGateway([]),
                role=Role.WORKER,
                system="s",
                user="u",
                tools=specs_for(("read_file",)),
                call_tool=lambda *a: None,
                finish=SubmitDev,
                max_steps=1,
            )
        )


# ── the gateway side of tool calling ──


class CapturingTransport:
    def __init__(self):
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return Completion(content="ok", model="m", prompt_tokens=1, completion_tokens=1)


def make_gateway(transport):
    settings = Settings(_env_file=None, model_api_key="k", max_output_tokens=64)
    return Gateway(settings, BudgetGuard(10_000), UsageLog(), transport=transport)


def test_the_gateway_forwards_tools_and_accepts_tool_messages():
    transport = CapturingTransport()
    gateway = make_gateway(transport)
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file text"},
    ]
    gateway.complete(Role.WORKER, messages, tools=TOOL_LIST)
    assert transport.calls[0]["tools"] is TOOL_LIST


@pytest.mark.parametrize(
    "bad",
    [
        {"role": "tool", "content": "x"},  # no tool_call_id
        {"role": "tool", "tool_call_id": "c1", "content": ""},
        {"role": "assistant", "content": ""},  # empty, and no tool calls
        {"role": "robot", "content": "x"},
        {"role": "user", "content": "x", "extra": 1},
    ],
)
def test_the_gateway_still_rejects_malformed_messages(bad):
    gateway = make_gateway(CapturingTransport())
    with pytest.raises(GatewayError):
        gateway.complete(Role.WORKER, [{"role": "user", "content": "u"}, bad])


def test_tool_parameters_are_strict_schemas_for_every_catalogue_tool():
    for spec in specs_for(tuple(TOOLS)):
        params = spec["function"]["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])
        assert "title" not in params
