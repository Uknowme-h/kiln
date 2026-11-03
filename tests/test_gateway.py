import pytest

from kiln.agents.roles import Role
from kiln.config import Settings
from kiln.gateway.budget import BudgetGuard
from kiln.gateway.client import Completion, Gateway
from kiln.gateway.errors import BudgetExceeded, GatewayError, TransientError
from kiln.gateway.usage import UsageLog


class FakeTransport:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _gateway(transport, ceiling=1_000, attempts=3):
    settings = Settings(
        _env_file=None,
        model_api_key="spark-key",
        max_output_tokens=64,
        project_token_ceiling=ceiling,
        run_token_ceiling=ceiling,
    )
    return Gateway(
        settings,
        BudgetGuard(ceiling),
        UsageLog(),
        transport=transport,
        max_attempts=attempts,
        backoff_seconds=0,
    )


def test_complete_sets_max_tokens_and_records_usage():
    completion = Completion(
        content="ok",
        model="muse-spark-1.3",
        prompt_tokens=10,
        completion_tokens=4,
    )
    transport = FakeTransport([completion])
    gateway = _gateway(transport)
    result = gateway.complete(Role.PLANNER, [{"role": "user", "content": "plan"}])
    assert result.content == "ok"
    assert transport.calls[0]["max_tokens"] == 64
    assert transport.calls[0]["model"] == "muse-spark-1.3"
    assert gateway.budget.snapshot()["used"] == 14
    assert gateway.usage.summary()["calls"] == 1


def test_with_no_ceiling_max_tokens_is_omitted():
    completion = Completion(
        content="ok", model="muse-spark-1.3", prompt_tokens=10, completion_tokens=4
    )
    transport = FakeTransport([completion])
    # default settings: max_output_tokens is None -> no ceiling
    settings = Settings(_env_file=None, model_api_key="spark-key")
    gateway = Gateway(settings, BudgetGuard(1_000), UsageLog(), transport=transport)
    result = gateway.complete(Role.PLANNER, [{"role": "user", "content": "plan"}])
    assert result.content == "ok"
    # no ceiling: the transport is handed max_tokens=None (OpenAITransport then
    # omits the field entirely from the API request)
    assert transport.calls[0]["max_tokens"] is None
    # and the budget still charges the measured usage (no reservation needed)
    assert gateway.budget.snapshot()["used"] == 14


def test_retries_transient_failures_then_records_one_call():
    completion = Completion(
        content="ok",
        model="muse-spark-1.3",
        prompt_tokens=3,
        completion_tokens=3,
    )
    transport = FakeTransport([TransientError("rate"), TransientError("rate"), completion])
    gateway = _gateway(transport)
    assert gateway.complete(Role.REPORTER, [{"role": "user", "content": "write"}]).content == "ok"
    assert len(transport.calls) == 3
    assert gateway.usage.summary()["calls"] == 1


def test_budget_is_released_when_every_attempt_fails():
    transport = FakeTransport([TransientError("down")] * 3)
    gateway = _gateway(transport, attempts=3)
    with pytest.raises(GatewayError):
        gateway.complete(Role.WORKER, [{"role": "user", "content": "build"}])
    assert gateway.budget.snapshot() == {
        "ceiling": 1000,
        "used": 0,
        "reserved": 0,
        "remaining": 1000,
    }


def test_call_is_refused_before_the_transport_when_the_ceiling_is_too_small():
    transport = FakeTransport([])
    gateway = _gateway(transport, ceiling=10)
    with pytest.raises(BudgetExceeded):
        gateway.complete(Role.PLANNER, [{"role": "user", "content": "plan"}], max_tokens=11)
    assert transport.calls == []


def test_unknown_usage_is_charged_at_the_reservation():
    completion = Completion(
        content="ok",
        model="muse-spark-1.3",
        prompt_tokens=0,
        completion_tokens=0,
        usage_known=False,
    )
    gateway = _gateway(FakeTransport([completion]))
    gateway.complete(Role.PLANNER, [{"role": "user", "content": "plan"}], max_tokens=20)
    assert gateway.budget.snapshot()["used"] == 20


def test_response_format_is_forwarded_to_the_transport():
    completion = Completion(
        content="{}",
        model="muse-spark-1.3",
        prompt_tokens=1,
        completion_tokens=1,
    )
    transport = FakeTransport([completion])
    gateway = _gateway(transport)
    fmt = {"type": "json_schema", "json_schema": {"name": "spec", "strict": True, "schema": {}}}
    gateway.complete(
        Role.REQUIREMENTS,
        [{"role": "user", "content": "specify"}],
        response_format=fmt,
    )
    assert transport.calls[0]["response_format"] is fmt


def test_response_format_defaults_to_none():
    completion = Completion(
        content="ok",
        model="muse-spark-1.3",
        prompt_tokens=1,
        completion_tokens=1,
    )
    transport = FakeTransport([completion])
    gateway = _gateway(transport)
    gateway.complete(Role.PLANNER, [{"role": "user", "content": "plan"}])
    assert transport.calls[0]["response_format"] is None


def test_tools_are_forwarded_and_tool_messages_validate():
    from kiln.gateway.client import ModelToolCall

    completion = Completion(
        content="",
        model="muse-spark-1.3",
        prompt_tokens=1,
        completion_tokens=1,
        tool_calls=(ModelToolCall(id="c1", name="read_file", arguments='{"path":"a.py"}'),),
    )
    transport = FakeTransport([completion])
    gateway = _gateway(transport)
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    result = gateway.complete(
        Role.WORKER,
        [{"role": "user", "content": "go"}],
        tools=tools,
    )
    assert result.tool_calls[0].name == "read_file"
    assert transport.calls[0]["tools"] is tools
    # a follow-up with tool role messages must validate
    gateway.transport = FakeTransport(
        [Completion(content="ok", model="m", prompt_tokens=1, completion_tokens=1)]
    )
    gateway.complete(
        Role.WORKER,
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
    )


def test_empty_content_without_tool_calls_is_a_clear_error():
    # the reasoning-budget failure mode: the API returns finish_reason=length
    # with empty content; surface it here, not as a JSON EOF far downstream
    completion = Completion(
        content="",
        model="muse-spark-1.3",
        prompt_tokens=1,
        completion_tokens=64,
        finish_reason="length",
    )
    gateway = _gateway(FakeTransport([completion]))
    with pytest.raises(GatewayError, match="returned no content"):
        gateway.complete(Role.PLANNER, [{"role": "user", "content": "plan"}])


def test_a_completion_is_recorded_in_the_transcript():
    from kiln.agents.transcript import TranscriptLog

    transcript = TranscriptLog()
    completion = Completion(
        content="the plan",
        model="muse-spark-1.3",
        prompt_tokens=5,
        completion_tokens=7,
    )
    transport = FakeTransport([completion])
    settings = Settings(_env_file=None, model_api_key="k", max_output_tokens=64)
    gateway = Gateway(
        settings, BudgetGuard(1_000), UsageLog(), transport=transport, transcript=transcript
    )
    messages = [{"role": "user", "content": "plan"}]
    gateway.complete(Role.PLANNER, messages, agent_id="planner")

    entries = transcript.for_agent("planner")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "model_call"
    assert entry.payload["content"] == "the plan"
    assert entry.payload["messages"] == messages
    assert entry.payload["total_tokens"] == 12
    assert entry.payload["truncated"] is False


def test_transcript_defaults_to_role_when_no_agent_id():
    from kiln.agents.transcript import TranscriptLog

    transcript = TranscriptLog()
    completion = Completion(content="x", model="m", prompt_tokens=1, completion_tokens=1)
    settings = Settings(_env_file=None, model_api_key="k", max_output_tokens=64)
    gateway = Gateway(
        settings, BudgetGuard(1_000), UsageLog(),
        transport=FakeTransport([completion]), transcript=transcript,
    )
    gateway.complete(Role.REPORTER, [{"role": "user", "content": "go"}])
    assert transcript.agents() == ["reporter"]

