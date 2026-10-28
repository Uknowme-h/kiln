import json

import pytest

from kiln.agents import test_author
from kiln.agents.roles import Role
from kiln.gateway.client import Completion
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite

SPEC_PAYLOAD = {
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
        },
        {
            "id": "c2",
            "story_id": "s1",
            "given": "two tasks",
            "when": "I list them",
            "then": "both come back",
            "oracle": "tests/test_tasks.py — listing returns every task",
        },
    ],
    "file_plan": [{"path": "app.py", "purpose": "the app"}],
    "assumptions": [],
}

VALID_SUITE = {
    "spec_title": "Tasks",
    "files": [
        {
            "path": "tests/test_tasks.py",
            "content": "def test_add_persists():\n    ...\n"
            "def test_list_returns_all():\n    ...\n",
            "covers": ["c1", "c2"],
        }
    ],
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


def _spec() -> Spec:
    return Spec.model_validate(SPEC_PAYLOAD)


def test_a_valid_reply_validates_into_a_suite():
    gateway = FakeGateway([json.dumps(VALID_SUITE)])
    suite = test_author.run(gateway, _spec())
    assert isinstance(suite, TestSuite)
    assert suite.files[0].covers == ["c1", "c2"]
    assert len(gateway.calls) == 1


def test_the_call_uses_the_test_author_route_and_structured_output():
    gateway = FakeGateway([json.dumps(VALID_SUITE)])
    test_author.run(gateway, _spec())
    call = gateway.calls[0]
    assert call["role"] is Role.TEST_AUTHOR
    assert call["response_format"]["json_schema"]["name"] == "test_suite"


def test_coverage_is_enforced_against_the_spec():
    missing_c2 = {
        "spec_title": "Tasks",
        "files": [
            {"path": "tests/test_tasks.py", "content": "def test_x():\n    ...\n", "covers": ["c1"]}
        ],
    }
    gateway = FakeGateway([json.dumps(missing_c2), json.dumps(VALID_SUITE)])
    suite = test_author.run(gateway, _spec())
    assert isinstance(suite, TestSuite)
    assert len(gateway.calls) == 2
    retry_message = gateway.calls[1]["messages"][1]["content"]
    assert "validation_error" in retry_message
    assert "no test covers" in retry_message  # the real reason is shown


def test_two_invalid_replies_are_counted_as_a_failure():
    gateway = FakeGateway(['{"files": []}', '{"nope": true}'])
    with pytest.raises(test_author.TestAuthorError):
        test_author.run(gateway, _spec())
    assert len(gateway.calls) == 2
