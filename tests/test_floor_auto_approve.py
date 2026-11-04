"""The floor's "allow all" switch: wired from the web UI to the companion.

Same reach as --auto-approve: command approvals run unattended, while the plan
beat and ask_user questions still reach the user. Auto-resolved requests are
tagged so the floor logs them instead of flashing a dialog.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from kiln.companion.client import _dispatch
from kiln.companion.executor import Executor
from kiln.companion.permissions import PermissionDenied, PermissionEngine
from kiln.companion.prompt import ApprovalPrompt, ApprovalRequest
from kiln.companion.protocol import ApprovalAnswer
from kiln.companion.registry import CompanionRegistry, CompanionUnavailable
from kiln.config import Settings
from kiln.main import create_app


class _RemotePrompt(ApprovalPrompt):
    """Records the floor's answer instead of racing a real terminal."""

    def __init__(self):
        super().__init__(interactive=False, output=lambda _: None)
        self.remote_replies = []
        self.ask_calls = 0

    def answer_remotely(self, reply):
        self.remote_replies.append(reply)
        return True

    def ask(self, request):
        self.ask_calls += 1
        return super().ask(request)  # non-interactive -> denied


def _executor(tmp_path, prompt):
    return Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=prompt)


def test_allow_all_answers_this_one_and_turns_auto_approve_on(tmp_path):
    prompt = _RemotePrompt()
    executor = _executor(tmp_path, prompt)
    assert executor.permissions.auto_approve is False

    assert executor.answer_approval(ApprovalAnswer(approved=True, scope="all"))
    assert executor.permissions.auto_approve is True
    reply = prompt.remote_replies[-1]
    assert reply.approved is True
    assert reply.scope == "run"  # the prompt coerces it to once if not rememberable


def test_a_deny_carrying_scope_all_never_turns_auto_approve_on(tmp_path):
    prompt = _RemotePrompt()
    executor = _executor(tmp_path, prompt)
    executor.answer_approval(ApprovalAnswer(approved=False, scope="all", feedback="no"))
    assert executor.permissions.auto_approve is False
    assert prompt.remote_replies[-1].scope == "once"


def test_the_floor_can_turn_it_off_again(tmp_path):
    executor = _executor(tmp_path, _RemotePrompt())
    executor.set_auto_approve(True)
    executor.set_auto_approve(False)
    assert executor.permissions.auto_approve is False


def test_plan_still_asks_after_the_floor_enables_allow_all(tmp_path):
    prompt = _RemotePrompt()
    executor = _executor(tmp_path, prompt)
    executor.set_auto_approve(True)
    with pytest.raises(PermissionDenied):
        executor.execute("confirm_plan", {"lines": ["3 tasks"], "round": 1})
    assert prompt.ask_calls == 1


def test_auto_approved_requests_are_tagged_for_the_floor():
    engine = PermissionEngine(_RemotePrompt(), auto_approve=True)
    sent = []
    engine.authorize(
        agent_id="worker-1",
        agent_label="Wren",
        action="shell",
        resource="pytest -q",
        request=ApprovalRequest("Wren", "shell", ["pytest -q"]),
        notify=sent.append,
    )
    assert [p.state for p in sent] == ["awaiting_approval", "approval_resolved"]
    assert all(p.auto_approved for p in sent)


def test_manual_requests_are_not_tagged():
    engine = PermissionEngine(_RemotePrompt(), auto_approve=False)
    sent = []
    with pytest.raises(PermissionDenied):
        engine.authorize(
            agent_id="worker-1",
            agent_label="Wren",
            action="shell",
            resource="pytest -q",
            notify=sent.append,
        )
    assert sent and not any(p.auto_approved for p in sent)


def test_companion_dispatch_applies_the_floor_toggle(tmp_path):
    executor = _executor(tmp_path, _RemotePrompt())

    async def go():
        outbox = asyncio.Queue()
        _dispatch(json.dumps({"type": "auto_approve", "enabled": True}), executor, outbox)
        assert executor.permissions.auto_approve is True
        _dispatch(json.dumps({"type": "auto_approve", "enabled": False}), executor, outbox)
        assert executor.permissions.auto_approve is False

    asyncio.run(go())


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def test_registry_relays_the_toggle_and_tracks_state():
    async def go():
        registry = CompanionRegistry()
        run = registry.create_run("a to-do app")
        with pytest.raises(CompanionUnavailable):
            await registry.set_auto_approve(run.id, True)
        socket = _FakeSocket()
        registry.attach(run, socket, "D:\\work", {}, auto_approve=False)
        await registry.set_auto_approve(run.id, True)
        assert socket.sent[-1] == {"type": "auto_approve", "enabled": True}
        assert run.auto_approve is True
        await registry.set_auto_approve(run.id, False)
        assert run.auto_approve is False
        # "Allow all" from the dialog flips the tracked state too
        await registry.answer_approval(run.id, True, scope="all")
        assert socket.sent[-1]["scope"] == "all"
        assert run.auto_approve is True

    asyncio.run(go())


def _client() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                _env_file=None,
                model_api_key="spark-key",
                project_token_ceiling=5000,
                pipeline_autostart=False,
            )
        )
    )


def test_auto_approve_endpoint_needs_a_run_and_a_companion():
    client = _client()
    missing = client.post("/runs/run-nope/auto-approve", json={"enabled": True}).json()
    assert missing["error"] == "unknown run"
    run = client.post("/runs", json={"idea": "a bookstore"}).json()
    lonely = client.post(f"/runs/{run['run_id']}/auto-approve", json={"enabled": True}).json()
    assert lonely["ok"] is False
    assert "no companion" in lonely["error"]
    assert client.get(f"/runs/{run['run_id']}").json()["auto_approve"] is False


def test_hello_reports_a_companion_started_with_auto_approve():
    client = _client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    with client.websocket_connect("/companion/ws") as ws:
        ws.send_json(
            {
                "type": "hello",
                "code": created["pair_code"],
                "workspace": "D:\\work\\todo",
                "version": "0.1.0",
                "auto_approve": True,
            }
        )
        assert ws.receive_json()["type"] == "hello_ack"
        assert client.app.state.runs.get_run(created["run_id"]).auto_approve is True
