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
