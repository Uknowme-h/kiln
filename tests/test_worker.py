import json

import pytest
from pydantic import ValidationError

from kiln.agents import worker
from kiln.agents.roles import Role
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


def test_the_call_uses_the_worker_route_and_raised_output_budget():
    gateway = FakeGateway([json.dumps(VALID_RESULT)])
    worker.run(gateway, TASK, SPEC, SUITE)
    call = gateway.calls[0]
    assert call["role"] is Role.WORKER
    assert call["response_format"]["json_schema"]["name"] == "worker_result"
    assert call["max_tokens"] == 12_288


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
