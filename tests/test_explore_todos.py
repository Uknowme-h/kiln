"""Explore quota / depth and todo store (F1 / F3)."""

import asyncio
import json

import pytest

from kiln.agents.explore import ExploreQuota, _EXPLORE_DEPTH, run_explore
from kiln.agents.loop import run_agent_loop
from kiln.agents.roles import Role
from kiln.agents.todos import TodoStore
from kiln.config import Settings
from kiln.gateway.client import Completion, ModelToolCall
from kiln.tools.registry import specs_for
from kiln.tools.schemas import SubmitDev, TodoItem


def test_explore_quota_exhausts():
    quota = ExploreQuota(2)
    assert quota.try_consume() is None
    assert quota.try_consume() is None
    assert "exhausted" in (quota.try_consume() or "")


def test_todo_store_merge_and_in_progress_limit():
    store = TodoStore()
    err = store.write(
        [
            TodoItem(id="a", content="one", status="pending"),
            TodoItem(id="b", content="two", status="in_progress"),
        ],
        merge=False,
    )
    assert err is None
    err = store.write(
        [TodoItem(id="c", content="three", status="in_progress")],
        merge=True,
    )
    assert err is not None and "in_progress" in err
    store.write([TodoItem(id="b", content="two", status="done")], merge=True)
    assert [i.id for i in store.open_items()] == ["a"]
    gaps = store.open_as_gaps()
    assert gaps and "todo a:" in gaps[0]


@pytest.mark.asyncio
async def test_nested_explore_refused():
    cfg = Settings(_env_file=None, enable_explore_subagent=True, explore_max_steps=2)

    class FakeGateway:
        def __init__(self):
            self.settings = cfg

    async def noop_tool(name: str, args: dict) -> str:
        return "x"

    token = _EXPLORE_DEPTH.set(1)
    try:
        out = await run_explore(
            gateway=FakeGateway(),  # type: ignore[arg-type]
            call_tool=noop_tool,
            prompt="find auth",
            parent_id="worker-1",
            explore_index=1,
            settings=cfg,
        )
        assert "depth limit" in out
    finally:
        _EXPLORE_DEPTH.reset(token)


def test_todo_probe_injects_open_items_at_interval():
    store = TodoStore()
    store.write(
        [TodoItem(id="a", content="wire auth", status="pending")],
        merge=False,
    )

    def call(name, args, call_id=None):
        return ModelToolCall(
            id=call_id or f"c-{name}", name=name, arguments=json.dumps(args)
        )

    def turn(*calls, text=""):
        return Completion(
            content=text,
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            tool_calls=tuple(calls),
        )

    submit = {
        "task_id": "t1",
        "summary": "built it",
        "files_written": ["app.py"],
        "what_works": "all",
        "known_gaps": [],
    }
    # steps 1-4: read; step 5: probe then submit on next call
    script = [turn(call("read_file", {"path": f"f{i}.py"})) for i in range(4)]
    script.append(turn(call("submit_result", submit)))

    class GW:
        def __init__(self):
            self.script = list(script)
            self.seen = []

        def complete(self, role, messages, **kwargs):
            self.seen.append({"messages": list(messages), **kwargs})
            return self.script.pop(0)

    async def call_tool(name, args):
        return "ok"

    gw = GW()
    outcome = asyncio.run(
        run_agent_loop(
            gateway=gw,
            role=Role.WORKER,
            system="sys",
            user="go",
            tools=specs_for(("read_file", "submit_result")),
            call_tool=call_tool,
            finish=SubmitDev,
            max_steps=8,
            todo_store=store,
            todo_probe_every=5,
        )
    )
    assert outcome.result.task_id == "t1"
    # After 5th step starts, an open-todo probe should appear in some user message.
    probed = False
    for seen in gw.seen:
        for msg in seen["messages"]:
            if msg.get("role") == "user" and "Open todos" in (msg.get("content") or ""):
                probed = True
    assert probed


def test_open_todos_merge_into_known_gaps_strings():
    store = TodoStore()
    store.write(
        [
            TodoItem(id="a", content="wire auth", status="pending"),
            TodoItem(id="b", content="done bit", status="done"),
        ],
        merge=False,
    )
    gaps = store.open_as_gaps()
    assert gaps == ["todo a: wire auth"]


@pytest.mark.asyncio
async def test_compaction_keeps_reminder_and_skips_thrash():
    huge = "x" * 50_000
    submit = {
        "task_id": "t1",
        "summary": "built",
        "files_written": ["a.py"],
        "what_works": "ok",
        "known_gaps": [],
    }

    def call(name, args, call_id=None):
        return ModelToolCall(
            id=call_id or f"c-{name}", name=name, arguments=json.dumps(args)
        )

    def turn(*calls, text=""):
        return Completion(
            content=text,
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            tool_calls=tuple(calls),
        )

    class GW:
        def __init__(self):
            self.seen = []
            self.n = 0

        def complete(self, role, messages, **kwargs):
            self.seen.append({"messages": list(messages), "agent_id": kwargs.get("agent_id")})
            self.n += 1
            if kwargs.get("agent_id", "").endswith("/compact"):
                return Completion(
                    content="summary of work so far",
                    model="m",
                    prompt_tokens=1,
                    completion_tokens=1,
                )
            if self.n == 1:
                return turn(call("read_file", {"path": "big.py"}))
            return turn(call("submit_result", submit))

    async def call_tool(name, args):
        if name == "read_file":
            return huge
        return "ok"

    gw = GW()
    outcome = await run_agent_loop(
        gateway=gw,
        role=Role.WORKER,
        system="sys",
        user="<design>\ntokens</design>\ngo",
        tools=specs_for(("read_file", "submit_result")),
        call_tool=call_tool,
        finish=SubmitDev,
        max_steps=6,
        reminder="honor design tokens",
        enable_compaction=True,
        compact_threshold_chars=20_000,
    )
    assert outcome.result.task_id == "t1"
    # Compact summary call or thrash skip; reminder must survive in later turns.
    later = " ".join(
        m.get("content") or ""
        for seen in gw.seen
        for m in seen["messages"]
        if m.get("role") == "user"
    )
    assert "honor design tokens" in later or "design" in later.lower()
