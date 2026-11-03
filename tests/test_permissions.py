"""Permission engine: last-match-wins, deny precedence, and approval replies."""

from kiln.companion.permissions import PermissionDenied, PermissionEngine
from kiln.companion.prompt import ApprovalPrompt, ApprovalRequest, Reply
from kiln.permissions import DEFAULT_RULES, Rule, evaluate, role_rules, wildcard_match


def test_wildcard_and_last_match_wins():
    assert wildcard_match("app/main.py", "app/*")
    assert not wildcard_match("tests/x.py", "app/*")
    rules = (
        Rule("edit", "*", "allow"),
        Rule("edit", "tests/*", "deny"),
    )
    assert evaluate("edit", "app/main.py", rules).effect == "allow"
    assert evaluate("edit", "tests/test_x.py", rules).effect == "deny"


def test_default_rules_ask_for_shell_and_allow_reads():
    assert evaluate("read", "app.py", DEFAULT_RULES).effect == "allow"
    assert evaluate("read", ".env", DEFAULT_RULES).effect == "ask"
    assert evaluate("shell", "pytest -q", DEFAULT_RULES).effect == "ask"
    assert evaluate("install", "pip install x", DEFAULT_RULES).effect == "ask"


def test_role_rules_deny_undeclared_tools():
    reporter = role_rules(edit=("README.md", "docs/*"))
    assert evaluate("shell", "ls", reporter).effect == "deny"
    assert evaluate("edit", "README.md", reporter).effect == "allow"
    assert evaluate("edit", "app.py", reporter).effect == "deny"


def test_engine_denies_without_a_tty():
    engine = PermissionEngine(ApprovalPrompt(interactive=False))
    try:
        engine.authorize(
            agent_id="w1",
            agent_label="Worker 1",
            action="shell",
            resource="pytest -q",
        )
        raise AssertionError("expected PermissionDenied")
    except PermissionDenied as exc:
        assert "denied" in str(exc).lower() or "interactive" in str(exc).lower()


def test_engine_remembers_a_run_scoped_grant():
    replies = [Reply(True, "run")]

    def ask(_request):
        return replies.pop(0)

    prompt = ApprovalPrompt(interactive=True, output=lambda _: None)
    prompt.ask = ask  # type: ignore[method-assign]
    engine = PermissionEngine(prompt)
    request = ApprovalRequest(
        "Worker", "shell", ["pytest -q"], allow_run_scope=True
    )
    engine.authorize(
        agent_id="w1",
        agent_label="Worker",
        action="shell",
        resource="pytest -q",
        remember_key="pytest -q",
        request=request,
    )
    # second call must not prompt again
    def boom(_request):
        raise AssertionError("should not prompt again")

    prompt.ask = boom  # type: ignore[method-assign]
    engine.authorize(
        agent_id="w1",
        agent_label="Worker",
        action="shell",
        resource="pytest -q",
        remember_key="pytest -q",
        request=request,
    )


def test_user_refusal_carries_feedback():
    prompt = ApprovalPrompt(interactive=True, output=lambda _: None)
    prompt.ask = lambda _r: Reply(False, feedback="use pytest instead")  # type: ignore[method-assign]
    engine = PermissionEngine(prompt)
    try:
        engine.authorize(
            agent_id="w1",
            agent_label="Worker",
            action="shell",
            resource="rm -rf /",
            request=ApprovalRequest("Worker", "shell", ["rm -rf /"]),
        )
        raise AssertionError("expected PermissionDenied")
    except PermissionDenied as exc:
        assert "use pytest instead" in str(exc)
