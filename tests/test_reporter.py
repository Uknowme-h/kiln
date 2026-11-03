import asyncio
import json

import pytest

from kiln.agents import reporter
from kiln.agents.roles import Role
from kiln.gateway.client import Completion, ModelToolCall
from kiln.schemas.report import ReportBundle
from kiln.schemas.results import TestCaseResult, TestReport
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import TaskGraph
from kiln.schemas.work import WorkerResult

SPEC = Spec.model_validate(
    {
        "title": "Tasks",
        "summary": "A tiny task API.",
        "stack": "Python + Flask",
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

TASKS = TaskGraph.model_validate(
    {
        "spec_title": "Tasks",
        "tasks": [
            {
                "id": "t1",
                "title": "Tasks API",
                "description": "Flask endpoints",
                "owned_paths": ["app.py"],
            }
        ],
    }
)

WORKER_RESULTS = [
    WorkerResult.model_validate(
        {
            "task_id": "t1",
            "summary": "Flask app with POST and GET /tasks.",
            "files": [{"path": "app.py", "content": "from flask import Flask\n"}],
            "what_works": "POST stores, GET lists.",
            "known_gaps": ["no input validation beyond title presence"],
        },
        context={"task_id": "t1", "owned_paths": ["app.py"]},
    )
]

REPORT = TestReport(
    cases=[
        TestCaseResult(nodeid="tests/test_tasks.py::test_post", outcome="passed"),
        TestCaseResult(nodeid="tests/test_tasks.py::test_list", outcome="passed"),
    ]
)

USAGE = {
    "calls": 4,
    "prompt_tokens": 3000,
    "completion_tokens": 9000,
    "by_role": {"worker": {"calls": 1, "prompt_tokens": 1000, "completion_tokens": 4000}},
}

VALID_BUNDLE = {
    "readme": "# Tasks\n\nA tiny Flask task API.\n\n## Run\n\npython app.py\n",
    "run_summary": "GREEN. All 2 tests passed, 0 failed, 0 skipped. One task, no fix rounds.",
    "cost_report": "4 calls, 12,000 tokens total (worker: 5,000).",
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


def test_a_valid_reply_validates_into_a_bundle():
    gateway = FakeGateway([json.dumps(VALID_BUNDLE)])
    bundle = reporter.run(gateway, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE)
    assert isinstance(bundle, ReportBundle)
    assert bundle.readme and bundle.run_summary and bundle.cost_report
    assert len(gateway.calls) == 1


def test_the_call_uses_the_reporter_route_and_no_per_role_cap():
    gateway = FakeGateway([json.dumps(VALID_BUNDLE)])
    reporter.run(gateway, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE)
    call = gateway.calls[0]
    assert call["role"] is Role.REPORTER
    assert call["response_format"]["json_schema"]["name"] == "report_bundle"
    # no per-role hard cap: the call defers to the global safety net in config
    assert call["max_tokens"] is None
    # the soft cap lives in the system prompt instead
    assert "brief cost summary" in call["messages"][0]["content"]


def test_the_artifacts_reach_the_prompt_as_untrusted_content():
    gateway = FakeGateway([json.dumps(VALID_BUNDLE)])
    reporter.run(gateway, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE)
    user_message = gateway.calls[0]["messages"][1]["content"]
    for block in ("<spec>", "<tasks>", "<worker_results>", "<test_report>", "<usage>"):
        assert block in user_message
    assert "passed=2 failed=0 errors=0 skipped=0 green=True" in user_message
    assert "no input validation beyond title presence" in user_message  # gaps reach the reporter


def test_an_invalid_reply_is_retried_once_with_the_error_shown():
    gateway = FakeGateway(['{"readme": ""}', json.dumps(VALID_BUNDLE)])
    bundle = reporter.run(gateway, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE)
    assert isinstance(bundle, ReportBundle)
    assert len(gateway.calls) == 2
    assert "validation_error" in gateway.calls[1]["messages"][1]["content"]


def test_two_invalid_replies_are_counted_as_a_failure():
    gateway = FakeGateway(['{"readme": ""}', '{"nope": true}'])
    with pytest.raises(reporter.ReporterError):
        reporter.run(gateway, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE)
    assert len(gateway.calls) == 2


# ── the agentic verification pass (item 6) ──


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


def test_the_agentic_reporter_verifies_with_read_only_tools_then_submits():
    gateway = LoopGateway(
        [
            _turn(_model_call("read_file", {"path": "app.py"})),
            _turn(_model_call("submit_result", VALID_BUNDLE)),
        ]
    )
    log = []

    async def call_tool(name, args):
        log.append((name, args))
        return "from flask import Flask"

    bundle = asyncio.run(
        reporter.run_agentic(
            gateway, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE, call_tool
        )
    )
    assert isinstance(bundle, ReportBundle)
    assert log == [("read_file", {"path": "app.py"})]
    # the tool list is read-only plus the finish tool
    names = [tool["function"]["name"] for tool in gateway.seen[0]["tools"]]
    assert set(names) == {"read_file", "glob", "grep", "submit_result"}
    # the seam-hunting rule rides the system prompt
    assert "seam" in gateway.seen[0]["messages"][0]["content"]


def test_the_review_verdict_reaches_the_reporter():
    from kiln.schemas.review import ReviewGap, ReviewVerdict

    verdict = ReviewVerdict(
        gaps=[ReviewGap(claim="user accounts", evidence="only localStorage")],
        summary="1 gap",
    )
    gateway = FakeGateway([json.dumps(VALID_BUNDLE)])
    reporter.run(gateway, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE, review=verdict)
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "<review>" in user_message
    assert "user accounts — observed: only localStorage" in user_message

    # skipped honestly when the suite never went green
    gateway2 = FakeGateway([json.dumps(VALID_BUNDLE)])
    reporter.run(gateway2, SPEC, TASKS, WORKER_RESULTS, REPORT, USAGE)
    assert "skipped" in gateway2.calls[0]["messages"][1]["content"]
