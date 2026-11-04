"""Auto-approve dive: shell/install approvals auto-run, critical questions don't.

The guarantee under test: the auto_approve flag lives on PermissionEngine and
covers ONLY its authorize()→ask() path (shell/install/web/test commands). Two
things must never be auto-answered:
- ask_user questionnaires (ApprovalPrompt.ask_questions — a different method),
- the plan-approval beat (executor._confirm_plan — a different funnel).
"""

import pytest

from kiln.companion.executor import Executor
from kiln.companion.permissions import PermissionDenied, PermissionEngine
from kiln.companion.prompt import ApprovalPrompt, ApprovalRequest, Question
from kiln.config import Settings


class _CountingPrompt(ApprovalPrompt):
    """Non-interactive prompt that records every ask; denies if actually hit."""

    def __init__(self):
        super().__init__(interactive=False, output=lambda _: None)
        self.ask_calls = 0
        self.question_calls = 0

    def ask(self, request):
        self.ask_calls += 1
        return super().ask(request)  # non-interactive, no remote -> denied

    def ask_questions(self, agent_label, questions):
        self.question_calls += 1
        return super().ask_questions(agent_label, questions)


def test_auto_approve_runs_shell_without_prompting():
    prompt = _CountingPrompt()
    engine = PermissionEngine(prompt, auto_approve=True)
    request = ApprovalRequest("Wren", "shell", ["pytest -q"], allow_run_scope=True)
    # must not raise and must not have hit the (denying) prompt
    engine.authorize(
        agent_id="worker-1",
        agent_label="Wren",
        action="shell",
        resource="pytest -q",
        request=request,
    )
    assert prompt.ask_calls == 0
    assert engine.audit[-1].effect == "auto-allow"


def test_without_auto_approve_a_shell_prompt_still_asks_and_denies():
    prompt = _CountingPrompt()
    engine = PermissionEngine(prompt, auto_approve=False)
    with pytest.raises(PermissionDenied):
        engine.authorize(
            agent_id="worker-1",
            agent_label="Wren",
            action="shell",
            resource="pytest -q",
            request=ApprovalRequest("Wren", "shell", ["pytest -q"]),
        )
    assert prompt.ask_calls == 1


def test_auto_approve_never_answers_ask_user_questions():
    # ask_questions is a separate ApprovalPrompt method; the engine's flag
    # cannot reach it. A non-interactive prompt returns empty (skipped), never
    # a fabricated answer.
    prompt = _CountingPrompt()
    answers = prompt.ask_questions(
        "Riley", [Question("Stack", "python or node?", ["python", "node"])]
    )
    assert prompt.question_calls == 1
    assert answers == [[]]  # skipped, not auto-answered


def test_auto_approve_never_approves_the_plan_beat(tmp_path):
    """The plan beat routes through the same authorize() funnel as shell; it
    must still reach the user (here a non-interactive prompt, which denies)."""
    from kiln.companion.permissions import PermissionDenied as Denied

    prompt = _CountingPrompt()
    executor = Executor(
        tmp_path, Settings(_env_file=None, sandbox="host", auto_approve=True), prompt=prompt
    )
    with pytest.raises(Denied):
        executor.execute("confirm_plan", {"lines": ["3 tasks"], "round": 1})
    assert prompt.ask_calls == 1  # the user was asked, not bypassed


def test_auto_approve_honors_run_scope_grants():
    prompt = _CountingPrompt()
    engine = PermissionEngine(prompt, auto_approve=True)
    for _ in range(2):
        engine.authorize(
            agent_id="worker-1",
            agent_label="Wren",
            action="shell",
            resource="pytest -q",
            request=ApprovalRequest("Wren", "shell", ["pytest -q"], allow_run_scope=True),
            remember_key="pytest -q",
        )
    assert prompt.ask_calls == 0
    assert engine.audit[-1].effect in ("auto-allow", "run-grant")


def test_executor_wires_auto_approve_from_settings(tmp_path):
    prompt = _CountingPrompt()
    executor = Executor(
        tmp_path,
        Settings(_env_file=None, sandbox="host", auto_approve=True),
        prompt=prompt,
    )
    assert executor.permissions.auto_approve is True
    executor_off = Executor(
        tmp_path,
        Settings(_env_file=None, sandbox="host"),
        prompt=_CountingPrompt(),
    )
    assert executor_off.permissions.auto_approve is False
