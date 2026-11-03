"""The brief-reviewer: evidence-backed verdicts, one-shot and agentic."""

import asyncio
import json

import pytest

from kiln.agents import reviewer
from kiln.agents.roles import Role
from kiln.gateway.client import Completion, ModelToolCall
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.review import ReviewVerdict
from kiln.schemas.spec import Spec

SPEC = Spec.model_validate(
    {
        "title": "Notes",
        "summary": "A note app with accounts.",
        "stack": "Python",
        "user_stories": [
            {"id": "s1", "as_a": "user", "i_want": "to sign in", "so_that": "my notes are mine"}
        ],
        "acceptance_criteria": [
            {
                "id": "c1",
                "story_id": "s1",
                "given": "an account",
                "when": "I log in",
                "then": "I see my notes",
                "oracle": "tests/test_auth.py — login returns a session cookie",
            }
        ],
        "file_plan": [{"path": "app.py", "purpose": "the app"}],
    }
)

REPORT = TestReport(
    cases=[
        TestCaseResult(nodeid="tests/test_auth.py::test_login", outcome="passed"),
        TestCaseResult(nodeid="tests/test_notes.py::test_list", outcome="passed"),
    ]
)

CLEAR = {"gaps": [], "summary": "the app delivers the brief"}

GAPS = {
    "gaps": [
        {
            "claim": "user accounts",
            "evidence": "app/auth.js stores users in localStorage; no server check",
            "paths": ["app/auth.js"],
        }
    ],
    "summary": "1 gap",
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


def test_a_valid_verdict_validates_and_carries_gaps():
    gateway = FakeGateway([json.dumps(GAPS)])
    verdict = reviewer.run(
        gateway, "a note app with accounts", SPEC, "## Auth model\n- demo", "", REPORT, {}
    )
    assert isinstance(verdict, ReviewVerdict)
    assert verdict.gaps[0].claim == "user accounts"
    assert verdict.gaps[0].evidence  # evidence is mandatory on every gap
    assert len(gateway.calls) == 1


def test_the_call_uses_the_reviewer_route():
    gateway = FakeGateway([json.dumps(CLEAR)])
    reviewer.run(gateway, "an idea", SPEC, "", "", REPORT, {})
    call = gateway.calls[0]
    assert call["role"] is Role.REVIEWER
    assert call["response_format"]["json_schema"]["name"] == "review_verdict"


def test_the_evidence_and_workspace_reach_the_prompt_as_untrusted_content():
    gateway = FakeGateway([json.dumps(CLEAR)])
    snapshot = {"app.py": "from flask import Flask\n", "README.md": "# Notes\n"}
    reviewer.run(
        gateway,
        "a note app with accounts",
        SPEC,
        "## Auth model\n- demo (explicitly labeled)",
        "## palette\n- ink on paper",
        REPORT,
        snapshot,
    )
    user_message = gateway.calls[0]["messages"][1]["content"]
    for block in (
        "<user_idea>",
        "<spec>",
        "<discovery>",
        "<design>",
        "<test_report>",
        "<app_files>",
    ):
        assert block in user_message
    assert "passed=2 failed=0 errors=0 skipped=0 green=True" in user_message
    assert '<file path="app.py">' in user_message
    assert "demo (explicitly labeled)" in user_message
    # idea content is wrapped, not trusted
    assert "<user_idea>\na note app with accounts\n</user_idea>" in user_message


def test_missing_discovery_and_design_say_so_instead_of_faking_them():
    gateway = FakeGateway([json.dumps(CLEAR)])
    reviewer.run(gateway, "an idea", SPEC, "", "", REPORT, {})
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "(no docs/discovery.md was written)" in user_message
    assert "(no docs/design.md" in user_message


def test_large_snapshots_are_trimmed_not_omitted():
    gateway = FakeGateway([json.dumps(CLEAR)])
    big = "x" * 10_000
    reviewer.run(gateway, "an idea", SPEC, "", "", REPORT, {"app.py": big})
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "# ... trimmed ..." in user_message
    assert len(user_message) < 20_000  # capped, not the whole file


def test_an_invalid_verdict_is_retried_once_with_the_error_shown():
    gateway = FakeGateway(['{"nope": true}', json.dumps(CLEAR)])
    verdict = reviewer.run(gateway, "an idea", SPEC, "", "", REPORT, {})
    assert isinstance(verdict, ReviewVerdict)
    assert len(gateway.calls) == 2
    assert "validation_error" in gateway.calls[1]["messages"][1]["content"]


def test_two_invalid_replies_are_counted_as_a_failure():
    gateway = FakeGateway(['{"nope": true}', '{"nope": true}'])
    with pytest.raises(reviewer.ReviewerError):
        reviewer.run(gateway, "an idea", SPEC, "", "", REPORT, {})
    assert len(gateway.calls) == 2


# ── the agentic verification pass (item 13) ──


def _model_call(name, args, call_id=None):
    return ModelToolCall(
        id=call_id or f"c-{name}", name=name, arguments=json.dumps(args)
    )


def _turn(*calls, text=""):
    return Completion(
        content=text, model="m", prompt_tokens=1, completion_tokens=1, tool_calls=tuple(calls)
    )


class LoopGateway:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def complete(self, role, messages, **kwargs):
        self.seen.append({"messages": list(messages), **kwargs})
        return self.script.pop(0)


def test_the_agentic_reviewer_reads_then_submits_the_verdict():
    gateway = LoopGateway(
        [
            _turn(_model_call("read_file", {"path": "app/auth.js"})),
            _turn(_model_call("submit_result", GAPS)),
        ]
    )
    log = []

    async def call_tool(name, args):
        log.append((name, args))
        return "localStorage.setItem('users', ...)"

    verdict = asyncio.run(
        reviewer.run_agentic(
            gateway, "a note app with accounts", SPEC, "## Auth model\n- demo", "", REPORT, {},
            call_tool,
        )
    )
    assert isinstance(verdict, ReviewVerdict)
    assert verdict.gaps[0].claim == "user accounts"
    assert log == [("read_file", {"path": "app/auth.js"})]
    # the tool list is read-only plus the finish tool — never write or shell
    names = [tool["function"]["name"] for tool in gateway.seen[0]["tools"]]
    assert set(names) == {"read_file", "list_dir", "glob", "grep", "submit_result"}
    # the evidence-only rule rides the system prompt
    assert "evidence" in gateway.seen[0]["messages"][0]["content"]
