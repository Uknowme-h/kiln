import json

import pytest

from kiln.agents import fixer
from kiln.agents.roles import Role
from kiln.gateway.client import Completion
from kiln.schemas.patch import FixPatch
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.tasks import Task

TASK = Task(
    id="t1",
    title="Accounts API",
    description="Sign-up and login endpoints",
    owned_paths=["app/accounts/"],
)

REPORT = TestReport(
    cases=[
        TestCaseResult(nodeid="tests/test_x.py::test_ok", outcome="passed"),
        TestCaseResult(
            nodeid="tests/test_accounts.py::test_login",
            outcome="failed",
            message="assert 404 == 200",
        ),
    ],
    raw_log="FAILED tests/test_accounts.py::test_login - assert 404 == 200",
)

CURRENT_FILES = {"app/accounts/routes.py": "def login():\n    return None\n"}

VALID_PATCH = {
    "diagnosis": "login route returns None, so Flask 404s; it must return the token JSON",
    "files": [
        {
            "path": "app/accounts/routes.py",
            "content": "def login():\n    return {'token': 'abc'}, 200\n",
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


def test_a_valid_reply_validates_into_a_patch():
    gateway = FakeGateway([json.dumps(VALID_PATCH)])
    patch = fixer.run(gateway, TASK, REPORT, CURRENT_FILES)
    assert isinstance(patch, FixPatch)
    assert patch.diagnosis
    assert patch.files[0].path == "app/accounts/routes.py"
    assert len(gateway.calls) == 1


def test_the_call_uses_the_fixer_route_and_structured_output():
    gateway = FakeGateway([json.dumps(VALID_PATCH)])
    fixer.run(gateway, TASK, REPORT, CURRENT_FILES)
    call = gateway.calls[0]
    assert call["role"] is Role.FIXER
    assert call["response_format"]["json_schema"]["name"] == "fix_patch"


def test_the_failing_cases_and_code_are_quoted_as_untrusted_content():
    gateway = FakeGateway([json.dumps(VALID_PATCH)])
    fixer.run(gateway, TASK, REPORT, CURRENT_FILES)
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "<test_report>" in user_message
    assert "assert 404 == 200" in user_message  # the evidence reaches the fixer
    assert '<current_files path="app/accounts/routes.py">' in user_message
    assert "<task>" in user_message


def test_a_patch_outside_the_owned_paths_is_rejected_and_retried():
    rogue = {
        "diagnosis": "rewrite the tests instead",
        "files": [{"path": "tests/test_accounts.py", "content": "def test_x():\n    pass\n"}],
    }
    gateway = FakeGateway([json.dumps(rogue), json.dumps(VALID_PATCH)])
    patch = fixer.run(gateway, TASK, REPORT, CURRENT_FILES)
    assert isinstance(patch, FixPatch)
    assert len(gateway.calls) == 2
    retry_message = gateway.calls[1]["messages"][1]["content"]
    assert "validation_error" in retry_message
    assert "outside the task's owned paths" in retry_message


def test_two_invalid_replies_are_counted_as_a_failure():
    gateway = FakeGateway(['{"files": []}', '{"nope": true}'])
    with pytest.raises(fixer.FixerError):
        fixer.run(gateway, TASK, REPORT, CURRENT_FILES)
    assert len(gateway.calls) == 2


def test_the_fixer_needs_current_files_before_spend():
    gateway = FakeGateway([])
    with pytest.raises(fixer.FixerError):
        fixer.run(gateway, TASK, REPORT, {})
    assert gateway.calls == []
