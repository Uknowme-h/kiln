"""ask_user payload on the wire + remote question answers."""

from __future__ import annotations

import threading
import time

from kiln.companion.prompt import ApprovalPrompt, Question
from kiln.companion.protocol import QuestionPayload, ToolProgress
from kiln.events.schema import ApprovalPending, decode_event, encode_event


def test_tool_progress_carries_questions():
    progress = ToolProgress(
        id="ask-1",
        state="awaiting_approval",
        agent="requirements",
        action="question",
        detail="waiting",
        questions=[
            QuestionPayload(
                header="Auth model",
                question="Sessions or demo?",
                options=["real sessions + hashed passwords", "demo"],
                multiple=False,
            )
        ],
    )
    assert progress.questions[0].header == "Auth model"
    assert len(progress.questions[0].options) == 2


def test_approval_pending_questions_round_trip_on_sse():
    event = ApprovalPending(
        agent_id="requirements",
        action="question",
        summary="Riley is waiting for your answers",
        questions=[
            {
                "header": "UI direction",
                "question": "Which look?",
                "options": ["editorial", "industrial", "soft garden"],
                "multiple": False,
            }
        ],
    )
    decoded = decode_event(encode_event(event))
    assert decoded.kind == "approval_pending"
    assert decoded.action == "question"
    assert decoded.questions[0].header == "UI direction"
    assert "editorial" in decoded.questions[0].options


def test_remote_question_answers_resolve_without_terminal():
    prompt = ApprovalPrompt(interactive=False, remote=True, output=lambda _: None)
    results: list[list[list[str]]] = []

    def ask() -> None:
        results.append(
            prompt.ask_questions(
                "Riley",
                [Question("Auth model", "Sessions or demo?", ["real sessions", "demo"])],
            )
        )

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if prompt.answer_questions_remotely([["real sessions"]]):
            break
        time.sleep(0.01)
    thread.join(timeout=5)
    assert results == [[["real sessions"]]]
