"""An agent that did its work must not lose it — or the run — at the finish line.

run-afe452e5: the backend scaffold worker wrote all four files of its task,
hit its 30-step cap, and at the single handoff turn sent submit_result with
EMPTY arguments. The rejection was silent, there was no second attempt, the
WorkerError propagated through the layer's gather and the whole run died with
finished work on disk. Its steps had gone to an offloaded read_file it
re-assembled over five turns, todo-only turns after every write, and
re-verifying its own files after compaction.

These tests pin every layer of the fix.
"""

import asyncio
import json
from dataclasses import replace

import pytest

from kiln.agents import fixer, worker
from kiln.agents.loop import HANDOFF_ATTEMPTS, AgentLoopError, run_agent_loop
from kiln.agents.roles import Role
from kiln.agents.todos import TodoStore
from kiln.agents.transcript import TranscriptLog
from kiln.config import Settings
from kiln.gateway.client import Completion, ModelToolCall
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import Task
from kiln.tools.registry import specs_for
from kiln.tools.schemas import SubmitDev, TodoItem

TOOLS = specs_for(("read_file", "write_file", "submit_result"))

SUBMIT = {
    "task_id": "t1",
    "summary": "built it",
    "files_written": ["app.py"],
    "what_works": "all of it",
    "known_gaps": [],
}


def call(name, args, call_id=None):
    return ModelToolCall(id=call_id or f"c-{name}", name=name, arguments=json.dumps(args))


def turn(*calls, text=""):
    return Completion(content=text, model="m", prompt_tokens=1, completion_tokens=1, tool_calls=tuple(calls))


class Scripted:
    def __init__(self, script, *, summary="## Objective\nkeep going"):
        self.script = list(script)
        self.seen = []
        self.summary = summary
        self.settings = Settings(
            _env_file=None, enable_explore_subagent=False, enable_todo_tool=False,
            post_edit_diagnostics=False,
        )

    def complete(self, role, messages, **kwargs):
        self.seen.append({"messages": list(messages), **kwargs})
        if str(kwargs.get("agent_id", "")).endswith("/compact"):
            return Completion(content=self.summary, model="m", prompt_tokens=1, completion_tokens=1)
        return self.script.pop(0)


def run(gateway, *, max_steps, transcript=None, todo_store=None, **kwargs):
    async def call_tool(name, args):
        return f"wrote {args['path']}" if name == "write_file" else "contents"

    return asyncio.run(
        run_agent_loop(
            gateway=gateway, role=Role.WORKER, system="sys", user="go", tools=TOOLS,
            call_tool=call_tool, finish=SubmitDev, max_steps=max_steps,
            transcript=transcript, agent_id="worker-1", todo_store=todo_store, **kwargs,
        )
    )


# ── the loop ─────────────────────────────────────────────────────────────────


def test_an_empty_handoff_gets_another_attempt_with_the_reason():
    """The exact run-afe452e5 sequence, now recovered."""
    transcript = TranscriptLog()
    gateway = Scripted([
        turn(call("write_file", {"path": "app.py", "content": "x"})),
        turn(call("submit_result", {})),          # the empty handoff
        turn(call("submit_result", SUBMIT)),      # second attempt, told what was missing
    ])
    outcome = run(gateway, max_steps=1, transcript=transcript)
    assert outcome.result.task_id == "t1"
    retry_prompt = gateway.seen[-1]["messages"]
    rejection = [m["content"] for m in retry_prompt if m["role"] == "tool"][-1]
    assert "rejected" in rejection and "task_id" in rejection and "Required fields" in rejection
    logged = [e for e in transcript.for_agent("worker-1") if e.payload.get("name") == "submit_result"]
    assert any(e.payload.get("error") for e in logged), "a rejected submit must be in the transcript"


def test_the_handoff_tells_the_agent_which_files_it_wrote():
    gateway = Scripted([
        turn(call("write_file", {"path": "app.py", "content": "x"})),
        turn(call("submit_result", SUBMIT)),
    ])
    run(gateway, max_steps=1)
    handoff = gateway.seen[-1]["messages"][-1]["content"]
    assert "out of steps" in handoff and "- app.py" in handoff


def test_a_loop_failure_carries_the_files_already_written():
    gateway = Scripted(
        [turn(call("write_file", {"path": "app.py", "content": "x"}))]
        + [turn(call("submit_result", {})) for _ in range(HANDOFF_ATTEMPTS)]
    )
    with pytest.raises(AgentLoopError, match="step cap") as info:
        run(gateway, max_steps=1)
    assert info.value.written_paths == ["app.py"]


def test_a_repeated_invalid_submit_gets_the_reason_not_a_doom_refusal():
    gateway = Scripted([turn(call("submit_result", {})) for _ in range(3)] + [turn(call("submit_result", SUBMIT))])
    outcome = run(gateway, max_steps=6)
    assert outcome.result.task_id == "t1"
    tool_texts = [m["content"] for m in gateway.seen[-1]["messages"] if m["role"] == "tool"]
    assert all("Refused" not in text for text in tool_texts)
    assert all("Required fields" in text for text in tool_texts)


def _open_todos():
    store = TodoStore()
    store.write([TodoItem(id="a", content="wire auth", status="pending")], merge=False)
    return store


def test_the_todo_probe_leaves_a_productive_agent_alone():
    writes = [turn(call("write_file", {"path": f"f{i}.py", "content": "x"})) for i in range(6)]
    gateway = Scripted(writes + [turn(call("submit_result", SUBMIT))])
    run(gateway, max_steps=10, todo_store=_open_todos())
    every_prompt = [m.get("content") or "" for seen in gateway.seen for m in seen["messages"]]
    assert not any("Goal probe" in text for text in every_prompt)


def test_the_todo_probe_still_nudges_a_stalled_agent():
    reads = [turn(call("read_file", {"path": f"f{i}.py"})) for i in range(5)]
    gateway = Scripted(reads + [turn(call("submit_result", SUBMIT))])
    run(gateway, max_steps=10, todo_store=_open_todos())
    every_prompt = [m.get("content") or "" for seen in gateway.seen for m in seen["messages"]]
    assert any("Goal probe" in text and "same turn" in text for text in every_prompt)


def test_compaction_carries_the_files_already_written():
    gateway = Scripted([
        turn(call("write_file", {"path": "app.py", "content": "x" * 400})),
        turn(call("read_file", {"path": "b.py"})),
        turn(call("submit_result", SUBMIT)),
    ])
    run(gateway, max_steps=5, compact_threshold_chars=300)
    after = [m.get("content") or "" for m in gateway.seen[-1]["messages"]]
    summary = next(text for text in after if "Conversation summary after compaction" in text)
    assert "Files you already wrote" in summary and "- app.py" in summary


# ── the worker salvages, the run survives ───────────────────────────────────

TASK = Task(id="t1", title="Tasks API", description="Flask app", owned_paths=["app.py"])
SPEC = Spec.model_validate({
    "title": "Tasks", "summary": "A tiny task API.", "stack": "Python",
    "user_stories": [{"id": "s1", "as_a": "user", "i_want": "to add tasks", "so_that": "I track them"}],
    "acceptance_criteria": [{
        "id": "c1", "story_id": "s1", "given": "a list", "when": "I POST /tasks",
        "then": "201", "oracle": "tests/test_tasks.py — POST returns 201",
    }],
    "file_plan": [{"path": "app.py", "purpose": "the app"}],
})


def _short_cap(monkeypatch, module, role, steps):
    real = module.definition_for(role)
    monkeypatch.setattr(module, "definition_for", lambda r: replace(real, max_steps=steps))


def test_a_worker_that_wrote_its_files_returns_an_honest_partial_result(monkeypatch):
    _short_cap(monkeypatch, worker, Role.WORKER, 1)
    gateway = Scripted(
        [turn(call("write_file", {"path": "app.py", "content": "app = 1\n"}))]
        + [turn(call("submit_result", {})) for _ in range(HANDOFF_ATTEMPTS)]
    )
    disk = {}

    async def call_tool(name, args):
        if name == "write_file":
            disk[args["path"]] = args["content"]
            return f"wrote {args['path']}"
        return disk.get(args.get("path"), "")

    result = asyncio.run(worker.run_agentic(gateway, TASK, SPEC, call_tool))
    assert [f.path for f in result.files] == ["app.py"]
    assert result.files[0].content == "app = 1\n"
    assert "Unverified" in result.what_works
    assert any("without a valid handoff" in gap for gap in result.known_gaps)


def test_a_worker_that_wrote_nothing_still_fails(monkeypatch):
    _short_cap(monkeypatch, worker, Role.WORKER, 1)
    gateway = Scripted(
        [turn(call("read_file", {"path": "app.py"}))]
        + [turn(call("submit_result", {})) for _ in range(HANDOFF_ATTEMPTS)]
    )

    async def call_tool(name, args):
        return "contents"

    with pytest.raises(worker.WorkerError):
        asyncio.run(worker.run_agentic(gateway, TASK, SPEC, call_tool))


def test_out_of_scope_writes_are_never_salvaged(monkeypatch):
    _short_cap(monkeypatch, worker, Role.WORKER, 1)
    gateway = Scripted(
        [turn(call("write_file", {"path": "tests/test_x.py", "content": "x"}))]
        + [turn(call("submit_result", {})) for _ in range(HANDOFF_ATTEMPTS)]
    )

    async def call_tool(name, args):
        return "x"

    with pytest.raises(worker.WorkerError):
        asyncio.run(worker.run_agentic(gateway, TASK, SPEC, call_tool))


def _disk_tools(disk):
    """A workspace-backed call_tool that answers glob and ranged reads like the companion."""
    async def call_tool(name, args):
        path = args.get("path", "")
        if name == "write_file":
            disk[path] = args["content"]
            return f"wrote {path}"
        if name == "glob":
            hits = [p for p in disk if p.startswith(path.rstrip("/") + "/")]
            return "\n".join(hits) or "(no matches)"
        if name == "read_file":
            if path not in disk:
                raise RuntimeError(f"no such file in the workspace: {path}")
            if args.get("limit"):
                return f"# lines 1-1 of 1\n{disk[path]}"
            return disk[path]
        return "ok"

    return call_tool


NO_CHANGE = {
    "task_id": "t1", "summary": "Existing files already satisfy the task; test passes.",
    "files_written": [], "what_works": "verified by the targeted test", "known_gaps": [],
}


def test_a_verified_no_change_handoff_is_a_valid_result():
    """run-7f74e69c's worker-5: deliverable already on disk, test run green, honest
    'no changes needed' — must succeed, not kill the run."""
    gateway = Scripted([turn(call("submit_result", NO_CHANGE))])
    disk = {"app.py": "app = 1\n"}
    result = asyncio.run(worker.run_agentic(gateway, TASK, SPEC, _disk_tools(disk)))
    assert [f.path for f in result.files] == ["app.py"]
    assert result.files[0].content == "app = 1\n"


def test_an_empty_handoff_with_no_deliverable_is_retried_not_fatal():
    gateway = Scripted([
        turn(call("submit_result", NO_CHANGE)),  # nothing exists yet → rejected in-loop
        turn(call("write_file", {"path": "app.py", "content": "app = 2\n"})),
        turn(call("submit_result", SUBMIT)),
    ])
    disk = {}
    result = asyncio.run(worker.run_agentic(gateway, TASK, SPEC, _disk_tools(disk)))
    assert result.files[0].content == "app = 2\n"
    rejection = [m["content"] for m in gateway.seen[1]["messages"] if m["role"] == "tool"][-1]
    assert "none of your owned files exist yet" in rejection


FIX_TASK = Task(id="t1", title="Accounts", description="login", owned_paths=["app/accounts/"])
RED = TestReport(
    cases=[TestCaseResult(nodeid="tests/test_a.py::test_login", outcome="failed", message="404")],
    raw_log="FAILED",
)


def test_a_fixer_that_patched_on_disk_returns_a_partial_patch(monkeypatch):
    _short_cap(monkeypatch, fixer, Role.FIXER, 1)
    gateway = Scripted(
        [turn(call("write_file", {"path": "app/accounts/routes.py", "content": "ok = 1\n"}))]
        + [turn(call("submit_result", {})) for _ in range(HANDOFF_ATTEMPTS)]
    )
    disk = {}

    async def call_tool(name, args):
        if name == "write_file":
            disk[args["path"]] = args["content"]
            return "wrote"
        return disk.get(args.get("path"), "def login(): ...\n")

    patch = asyncio.run(
        fixer.run_agentic(gateway, FIX_TASK, RED, call_tool, {"app/accounts/routes.py": "x"})
    )
    assert patch.files[0].path == "app/accounts/routes.py"
    assert "partial, unverified" in patch.diagnosis


# ── read_file returns content, never an offload stub ────────────────────────


def test_a_large_read_is_line_bounded_with_an_offset_hint_not_offloaded(tmp_path):
    from kiln.companion.executor import READ_MAX_CHARS, Executor

    lines = [f"line {i}: " + "x" * 60 + "\n" for i in range(600)]  # ~40k chars
    (tmp_path / "big.ts").write_text("".join(lines), encoding="utf-8")
    (tmp_path / "small.ts").write_text("export const x = 1;\n", encoding="utf-8")
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"))

    small = executor.execute("read_file", {"path": "small.ts"})
    assert small == "export const x = 1;\n"  # unchanged for normal files

    first = executor.execute("read_file", {"path": "big.ts"})
    assert "offloaded" not in first
    header, body = first.split("\n", 1)
    assert header.startswith("# lines 1-") and "continue with offset=" in header
    assert len(body) <= READ_MAX_CHARS
    assert body.startswith("line 0:") and body.endswith("\n")  # whole lines only

    offset = int(header.split("offset=")[1].rstrip(")"))
    rest = executor.execute("read_file", {"path": "big.ts", "offset": offset})
    assert f"line {offset - 1}:" in rest.split("\n", 1)[1].split("\n", 1)[0]
