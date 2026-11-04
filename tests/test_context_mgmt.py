"""Prune, offload, and compaction shape for agent context hygiene."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kiln.agents.context import PRUNED_PREFIX, prune_tool_results
from kiln.agents.loop import COMPACT_KEEP_TAIL, COMPACT_SYSTEM, run_agent_loop
from kiln.agents.roles import Role
from kiln.companion.offload import OFFLOAD_DIR, maybe_offload_tool_output
from kiln.config import Settings
from kiln.gateway.client import Completion, ModelToolCall
from kiln.tools.registry import specs_for
from kiln.tools.schemas import SubmitDev


def test_settings_enable_compaction_by_default():
    s = Settings(_env_file=None)
    assert s.enable_compaction is True
    assert s.tool_offload_chars == 8_000
    assert s.prune_protect_chars == 40_000


def test_prune_keeps_recent_tool_bodies_and_slims_old_ones():
    old = "x" * 2_000
    recent = "y" * 500
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "c1", "content": old},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "c2", "content": recent},
        {"role": "assistant", "content": "last", "tool_calls": []},
        {"role": "tool", "tool_call_id": "c3", "content": "z" * 500},
    ]
    n = prune_tool_results(messages, protect_chars=2_000, protect_turns=2)
    assert n >= 1
    assert messages[3]["content"].startswith(PRUNED_PREFIX)
    assert messages[3]["tool_call_id"] == "c1"
    # Last two assistant turns' tools stay intact
    assert messages[5]["content"] == recent
    assert messages[7]["content"] == "z" * 500


def test_prune_is_idempotent_and_skips_offloaded():
    body = "[tool output offloaded — 99 chars]\npath: .kiln/tool-out/a.txt\n"
    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": "q" * 800},
        {"role": "tool", "tool_call_id": "c2", "content": body},
    ]
    # No recent assistant turns → both candidates; offloaded skipped
    prune_tool_results(messages, protect_chars=0, protect_turns=0)
    assert messages[0]["content"].startswith(PRUNED_PREFIX)
    assert messages[1]["content"] == body
    again = prune_tool_results(messages, protect_chars=0, protect_turns=0)
    assert again == 0


def test_offload_writes_stub_under_kiln_tool_out(tmp_path: Path):
    big = "LINE\n" * 3_000
    stub = maybe_offload_tool_output(
        tmp_path,
        tool="shell",
        output=big,
        agent_id="worker-3",
        call_id="ab12",
        threshold=8_000,
    )
    assert stub.startswith("[tool output offloaded")
    assert f"path: {OFFLOAD_DIR}/" in stub
    assert "--- head ---" in stub
    assert "Re-read with read_file" in stub
    files = list((tmp_path / OFFLOAD_DIR).glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == big


def test_offload_skips_small_and_status_tools(tmp_path: Path):
    assert maybe_offload_tool_output(
        tmp_path, tool="shell", output="tiny", threshold=100
    ) == "tiny"
    big = "z" * 200
    assert (
        maybe_offload_tool_output(
            tmp_path, tool="claim_scope", output=big, threshold=50
        )
        == big
    )
    assert not (tmp_path / OFFLOAD_DIR).exists()


def test_loop_compacts_with_structured_prompt_and_keeps_tail():
    # The threshold measures the work after the brief (a big brief alone must
    # not compact): grow the history with several moderate reads — one giant
    # tool result would trip the thrash skip instead of compacting.
    brief = "brief " * 2_000  # ~12k chars, ignored by the threshold

    class GW:
        def __init__(self):
            self.n = 0
            self.seen: list[dict] = []

        def complete(self, role, messages, **kwargs):
            self.n += 1
            self.seen.append({"messages": list(messages), "agent_id": kwargs.get("agent_id")})
            if kwargs.get("agent_id", "").endswith("/compact"):
                assert any(
                    m.get("role") == "system"
                    and COMPACT_SYSTEM[:20] in (m.get("content") or "")
                    for m in messages
                )
                return Completion(
                    content="## Objective\nBuild.\n## Next\nSubmit.",
                    model="m",
                    prompt_tokens=1,
                    completion_tokens=1,
                )

            def turn(*calls):
                return Completion(
                    content="",
                    tool_calls=list(calls),
                    model="m",
                    prompt_tokens=1,
                    completion_tokens=1,
                )

            def call(name, args):
                return ModelToolCall(
                    id=f"c{self.n}", name=name, arguments=json.dumps(args)
                )

            submit = {
                "task_id": "t1",
                "summary": "done",
                "files_written": ["a.py"],
                "what_works": "ok",
                "known_gaps": [],
            }
            self.turns = getattr(self, "turns", 0) + 1
            if self.turns <= 5:
                return turn(call("read_file", {"path": f"f{self.n}.py"}))
            return turn(call("submit_result", submit))

    async def call_tool(name, args):
        if name == "read_file":
            return "x" * 1_500
        return "ok"

    gw = GW()

    async def _run():
        return await run_agent_loop(
            gateway=gw,
            role=Role.WORKER,
            system="sys",
            user="go " + brief,
            tools=specs_for(("read_file", "submit_result")),
            call_tool=call_tool,
            finish=SubmitDev,
            max_steps=8,
            reminder="honor contract",
            enable_compaction=True,
            compact_threshold_chars=5_000,
            prune_protect_chars=1_000,
        )

    outcome = asyncio.run(_run())
    assert outcome.result.task_id == "t1"
    compact_calls = [
        s for s in gw.seen if str(s.get("agent_id", "")).endswith("/compact")
    ]
    assert compact_calls
    post = [
        s for s in gw.seen if not str(s.get("agent_id", "")).endswith("/compact")
    ]
    assert post
    # the first model call carries the whole brief uncompacted
    assert not any("Conversation summary" in (m.get("content") or "") for m in post[0]["messages"])
    # after compaction: the summary, the files already read, bounded history
    last_msgs = post[-1]["messages"]
    assert "Files you already read this task" in last_msgs[1]["content"]
    assert last_msgs[0]["role"] == "system"
    assert any(
        "Conversation summary after compaction" in (m.get("content") or "")
        for m in last_msgs
    )
    first_after = next(
        s["messages"] for s in post
        if any("Conversation summary" in (m.get("content") or "") for m in s["messages"])
    )
    assert len(first_after) <= 2 + COMPACT_KEEP_TAIL
    # a second compaction re-wraps the original brief, never the first wrapper
    assert last_msgs[1]["content"].count("<original_task>") == 1
