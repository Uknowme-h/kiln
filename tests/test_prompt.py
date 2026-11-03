import threading

from kiln.companion.prompt import ApprovalPrompt, ApprovalRequest, Question, Reply


def test_parse_once_deny_and_run_scope():
    assert ApprovalPrompt._parse("y", True).approved is True
    assert ApprovalPrompt._parse("a", True).scope == "run"
    assert ApprovalPrompt._parse("a", False).approved is False
    reply = ApprovalPrompt._parse("n use pytest", True)
    assert reply.approved is False
    assert reply.feedback == "use pytest"


def test_non_interactive_denies():
    prompt = ApprovalPrompt(interactive=False)
    reply = prompt.ask(ApprovalRequest("Worker", "shell", ["pytest"]))
    assert reply.approved is False


def test_questions_resolve_numbers_or_free_text():
    answers = iter(["1", "custom answer"])
    prompt = ApprovalPrompt(
        input_fn=lambda _: next(answers),
        output=lambda _: None,
        interactive=True,
    )
    result = prompt.ask_questions(
        "Riley",
        [
            Question("UI", "API or UI?", ["API only", "UI too"]),
            Question("Lang", "Which language?", ["python", "node"]),
        ],
    )
    assert result[0] == ["API only"]
    assert result[1] == ["custom answer"]


def test_remote_answer_resolves_without_a_terminal():
    prompt = ApprovalPrompt(interactive=False, remote=True, output=lambda _: None)
    replies = []
    request = ApprovalRequest("Worker", "shell", ["pytest -q"])
    asker = threading.Thread(
        target=lambda: replies.append(prompt.ask(request)),
        daemon=True,
    )
    asker.start()
    # answer arrives while the prompt is parked waiting for input
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if prompt.answer_remotely(Reply(True, "run")):
            break
        time.sleep(0.01)
    asker.join(timeout=5)
    assert replies and replies[0].approved is True
    # run scope is coerced down when the request never allowed it
    assert replies[0].scope == "once"


def test_remote_answer_too_late_is_dropped():
    prompt = ApprovalPrompt(interactive=False, remote=True, output=lambda _: None)
    assert prompt.answer_remotely(Reply(True)) is False


def test_remote_questions_too_late_are_dropped():
    prompt = ApprovalPrompt(interactive=False, remote=True, output=lambda _: None)
    assert prompt.answer_questions_remotely([["x"]]) is False
