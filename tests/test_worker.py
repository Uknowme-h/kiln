import json

import pytest
from pydantic import ValidationError

from kiln.agents import worker
from kiln.agents.roles import Role
from kiln.config import Settings
from kiln.gateway.client import Completion
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Task
from kiln.schemas.work import WorkerResult

TASK = Task(
    id="t1",
    title="Tasks API",
    description="Flask app exposing POST /tasks and GET /tasks with an in-memory store.",
    owned_paths=["app.py"],
)

SPEC = Spec.model_validate(
    {
        "title": "Tasks",
        "summary": "A tiny task API.",
        "stack": "Python + Flask, in-memory store, pytest",
        "user_stories": [
            {"id": "s1", "as_a": "user", "i_want": "to add tasks", "so_that": "I track them"}
        ],
        "acceptance_criteria": [
            {
                "id": "c1",
                "story_id": "s1",
                "given": "an empty list",
                "when": "I POST /tasks",
                "then": "201 and stored",
                "oracle": "tests/test_tasks.py — POST returns 201 and persists",
            }
        ],
        "file_plan": [{"path": "app.py", "purpose": "the app"}],
    }
)

SUITE = TestSuite.model_validate(
    {
        "spec_title": "Tasks",
        "files": [
            {
                "path": "tests/test_tasks.py",
                "content": "def test_post():\n    ...\n",
                "covers": ["c1"],
            }
        ],
    },
    context={"criterion_ids": ["c1"]},
)

VALID_RESULT = {
    "task_id": "t1",
    "summary": "Flask app with POST /tasks and GET /tasks over an in-memory list.",
    "files": [
        {
            "path": "app.py",
            "content": "from flask import Flask, request, jsonify\n\napp = Flask(__name__)\n",
        }
    ],
    "what_works": "POST stores a task and returns 201; GET lists all tasks with 200.",
    "known_gaps": [],
}


class FakeGateway:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, role, messages, **kwargs):
        self.calls.append({"role": role, "messages": messages, **kwargs})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return Completion(
            content=step, model="muse-spark-1.3", prompt_tokens=1, completion_tokens=1
        )


# ── WorkerResult schema rules ──


def test_result_must_be_for_the_given_task():
    with pytest.raises(ValidationError, match="expected t1"):
        WorkerResult.model_validate(
            {**VALID_RESULT, "task_id": "t2"},
            context={"task_id": "t1", "owned_paths": ["app.py"]},
        )


def test_result_files_stay_inside_owned_paths_and_out_of_tests():
    rogue_tests = {**VALID_RESULT, "files": [{"path": "tests/x.py", "content": "x"}]}
    with pytest.raises(ValidationError, match="never write test files"):
        WorkerResult.model_validate(
            rogue_tests, context={"task_id": "t1", "owned_paths": ["app.py"]}
        )
    rogue_path = {**VALID_RESULT, "files": [{"path": "other/x.py", "content": "x"}]}
    with pytest.raises(ValidationError, match="outside the task's owned paths"):
        WorkerResult.model_validate(
            rogue_path, context={"task_id": "t1", "owned_paths": ["app.py"]}
        )


# ── the agent ──


def test_a_valid_reply_validates_into_a_worker_result():
    gateway = FakeGateway([json.dumps(VALID_RESULT)])
    result = worker.run(gateway, TASK, SPEC, SUITE)
    assert isinstance(result, WorkerResult)
    assert result.task_id == "t1"
    assert len(gateway.calls) == 1


def test_the_call_uses_the_worker_route_and_no_per_role_cap():
    gateway = FakeGateway([json.dumps(VALID_RESULT)])
    worker.run(gateway, TASK, SPEC, SUITE)
    call = gateway.calls[0]
    assert call["role"] is Role.WORKER
    assert call["response_format"]["json_schema"]["name"] == "worker_result"
    # no per-role hard cap: the call defers to the global safety net in config
    assert call["max_tokens"] is None
    # the soft cap lives in the system prompt instead
    assert "owned paths" in call["messages"][0]["content"]


def test_the_task_spec_and_suite_reach_the_prompt_as_untrusted_content():
    gateway = FakeGateway([json.dumps(VALID_RESULT)])
    worker.run(gateway, TASK, SPEC, SUITE)
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "<task>" in user_message and "<spec>" in user_message
    assert "<tests>" in user_message
    assert "tests/test_tasks.py" in user_message  # the suite is visible to code against


def test_an_invalid_reply_is_retried_once_with_the_error_shown():
    rogue = {**VALID_RESULT, "files": [{"path": "src/x.py", "content": "x"}]}
    gateway = FakeGateway([json.dumps(rogue), json.dumps(VALID_RESULT)])
    result = worker.run(gateway, TASK, SPEC, SUITE)
    assert isinstance(result, WorkerResult)
    assert len(gateway.calls) == 2
    retry_message = gateway.calls[1]["messages"][1]["content"]
    assert "validation_error" in retry_message
    assert "outside the task's owned paths" in retry_message


def test_two_invalid_replies_are_counted_as_a_failure():
    gateway = FakeGateway(['{"files": []}', '{"nope": true}'])
    with pytest.raises(worker.WorkerError):
        worker.run(gateway, TASK, SPEC, SUITE)
    assert len(gateway.calls) == 2


# ── per-layer regression feed and dependency references (items 1, 3) ──


def test_the_test_state_block_reaches_the_prompt():
    gateway = FakeGateway([json.dumps(VALID_RESULT)])
    worker.run(
        gateway,
        TASK,
        SPEC,
        SUITE,
        test_state='<test_state>\nknown_green: t0\n</test_state>',
    )
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "<test_state>" in user_message
    assert "known_green: t0" in user_message


def test_the_test_state_block_separates_regressions_from_expected_red():
    block = worker.test_state_block(["t0"], 4, ["t1"])
    assert "1 passing, 4 failing" in block
    assert "known_green: t0" in block
    assert "regressions: t1" in block
    assert "Red is expected" in block
    # nothing has run yet → no block for the first layer
    assert worker.test_state_block([], 0, []) == ""


def test_dependency_paths_are_offered_as_references_not_contents():
    block = worker._dependency_paths_block(["models.py"])
    assert "- models.py" in block
    assert "read_file" in block
    assert worker._dependency_paths_block([]) == ""
    assert worker._dependency_paths_block(None) == ""


# ── agentic prompt is lean (run-9f836de2 prompt bloat, Milestone C) ──


def _model_call(name, args, call_id=None):
    from kiln.gateway.client import ModelToolCall

    return ModelToolCall(
        id=call_id or f"c-{name}", name=name, arguments=json.dumps(args)
    )


def _turn(*calls, text=""):
    return Completion(
        content=text, model="m", prompt_tokens=1, completion_tokens=1,
        tool_calls=tuple(calls),
    )


class LoopGateway:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.settings = Settings(
            _env_file=None,
            enable_explore_subagent=False,
            enable_todo_tool=False,
            post_edit_diagnostics=False,
        )

    def complete(self, role, messages, **kwargs):
        self.seen.append({"messages": list(messages), **kwargs})
        return self.script.pop(0)


def _submit_dev():
    return {
        "task_id": "t1",
        "summary": "done",
        "files_written": ["app.py"],
        "what_works": "all",
        "known_gaps": [],
    }


def test_the_agentic_worker_gets_a_test_digest_not_bodies():
    """run-9f836de2 burned 3.27M worker prompt tokens; the agentic worker has
    read_file, so the suite must arrive as paths+ACs to read, not inline bodies."""
    import asyncio

    gateway = LoopGateway([_turn(_model_call("submit_result", _submit_dev()))])

    async def call_tool(name, args):
        return "file body"

    big_body = "def test_post():\n    assert True\n" + ("# padding\n" * 500)
    suite = TestSuite.model_validate(
        {
            "spec_title": "Tasks",
            "files": [{"path": "tests/test_tasks.py", "content": big_body, "covers": ["c1"]}],
        },
        context={"criterion_ids": ["c1"]},
    )
    asyncio.run(
        worker.run_agentic(gateway, TASK, SPEC, call_tool, suite=suite)
    )
    user = gateway.seen[0]["messages"][1]["content"]
    assert "tests/test_tasks.py" in user  # the path to read
    assert "read_file" in user  # the instruction to read it
    assert "padding" not in user  # the body is NOT inlined
    assert "covers your task" in user


def test_the_agentic_worker_gets_a_spec_excerpt_not_the_whole_spec():
    import asyncio

    gateway = LoopGateway([_turn(_model_call("submit_result", _submit_dev()))])

    async def call_tool(name, args):
        return "file body"

    asyncio.run(worker.run_agentic(gateway, TASK, SPEC, call_tool, suite=SUITE))
    user = gateway.seen[0]["messages"][1]["content"]
    assert "<spec_excerpt>" in user
    # the task's AC reaches the prompt, but not the full user-story JSON dump
    assert "c1" in user
    assert "user_stories" not in user


def test_the_agentic_reminder_uses_a_compact_contract_digest():
    import asyncio

    from kiln.schemas.tasks import Contract, ContractRoute

    gateway = LoopGateway([_turn(_model_call("submit_result", _submit_dev()))])

    async def call_tool(name, args):
        return "file body"

    contract = Contract(
        routes=[ContractRoute(method="POST", path="/tasks", request="x", response="y")],
        modules=["app.create() -> Flask"],
        shared_files=["app.py"],
    )
    asyncio.run(
        worker.run_agentic(gateway, TASK, SPEC, call_tool, suite=SUITE, contract=contract)
    )
    # the reminder rides the doom-refusal / cap handoff; find it via run_agent_loop's
    # reminder kwarg is not exposed, so assert the digest helper itself is compact
    digest = worker._contract_digest(contract)
    assert "POST /tasks" in digest
    assert "app.create() -> Flask" in digest
    assert "scaffold owns: app.py" in digest
    # far smaller than the pretty JSON (no request/response/indent noise)
    assert len(digest) < len(contract.model_dump_json(indent=2))
    assert "request" not in digest and "response" not in digest
