"""The 1006 root cause: a blocking model call on the server's event loop.

The server runs the pipeline, the companion websocket, uvicorn's keepalive
pings and every new handshake on ONE asyncio loop. If an async agent calls the
synchronous `gateway.complete` directly, the whole loop freezes for the full
model call (60s+ for a 19k-token task graph): pings go unanswered, the
companion's keepalive gives up, the socket closes with 1006, and a reconnect
times out "during opening handshake". run-a609a72c showed exactly that during
the planner's final task-graph calls.

These tests fail if any async agent entry point runs a model call on the loop.
"""

import asyncio
import json
import time

from kiln.agents import planner
from kiln.gateway.client import Completion
from kiln.schemas.spec import Spec

SPEC = Spec.model_validate(
    {
        "title": "Tasks",
        "summary": "A small task list.",
        "stack": "Python",
        "user_stories": [
            {"id": "s1", "as_a": "user", "i_want": "to add a task", "so_that": "I remember it"}
        ],
        "acceptance_criteria": [
            {
                "id": "c1",
                "story_id": "s1",
                "given": "a list",
                "when": "I add one",
                "then": "it is stored",
                "oracle": "tests/test_tasks.py — adding a task persists it",
            }
        ],
        "file_plan": [{"path": "app.py", "purpose": "the app"}],
    }
)

GRAPH = {
    "spec_title": "Tasks",
    "tasks": [
        {"id": "t1", "title": "API", "description": "REST endpoints", "owned_paths": ["app/"]}
    ],
}

MODEL_SECONDS = 1.5


class SlowGateway:
    """A model that takes MODEL_SECONDS per call, blocking like a real HTTP call."""

    def complete(self, role, messages, **kwargs):
        time.sleep(MODEL_SECONDS)
        if kwargs.get("tools"):
            # exploration turn: answer without tool calls so planning starts
            return Completion(content="ready to plan", model="fake", prompt_tokens=1, completion_tokens=1)
        return Completion(content=json.dumps(GRAPH), model="fake", prompt_tokens=1, completion_tokens=1)


async def _max_loop_stall(coro) -> float:
    """Run `coro` while a ticker measures the longest gap the loop went dark."""
    stalls: list[float] = []
    done = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        await coro
    finally:
        done.set()
        await tick
    return max(stalls, default=0.0)


def _real_gateway():
    from kiln.config import Settings
    from kiln.gateway.budget import BudgetGuard
    from kiln.gateway.client import Gateway
    from kiln.gateway.usage import UsageLog

    class Instant:
        def complete(self, **kwargs):
            return Completion(content="ok", model="fake", prompt_tokens=1, completion_tokens=1)

    settings = Settings(_env_file=None, model_api_key="k", run_token_ceiling=10_000)
    return Gateway(settings, BudgetGuard(10_000), UsageLog(), transport=Instant(), backoff_seconds=0)


def test_gateway_flags_a_model_call_made_on_the_event_loop(caplog):
    from kiln.agents.roles import Role

    gateway = _real_gateway()
    messages = [{"role": "user", "content": "hi"}]

    async def on_loop():
        gateway.complete(Role.PLANNER, messages)  # the bug: blocking on the loop

    async def off_loop():
        await asyncio.to_thread(gateway.complete, Role.PLANNER, messages)  # correct

    with caplog.at_level("ERROR", logger="kiln.gateway"):
        asyncio.run(off_loop())
    assert not [r for r in caplog.records if "event loop thread" in r.getMessage()]

    with caplog.at_level("ERROR", logger="kiln.gateway"):
        asyncio.run(on_loop())
    assert [r for r in caplog.records if "event loop thread" in r.getMessage()]


def test_agentic_planner_never_blocks_the_event_loop():
    async def noop_tool(name, args):
        return ""

    stall = asyncio.run(
        _max_loop_stall(
            planner.run_agentic(SlowGateway(), SPEC, call_tool=noop_tool)
        )
    )
    # every model call takes 1.5s; the loop must keep ticking through all of
    # them. A stall near MODEL_SECONDS means a call ran on the loop thread.
    assert stall < 0.5, f"event loop was blocked for {stall:.2f}s during planning"
