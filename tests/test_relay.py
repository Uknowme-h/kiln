"""HTTP and websocket paths for runs and pairing.

The full duplex relay (tool_call out, tool_result back) is covered end to end
by scripts/smoke_companion.py against a real server; the registry mechanics
are covered in test_registry.py. TestClient cannot interleave a held-open
websocket with a concurrent request from a second client, so that path is not
exercised here.
"""

from fastapi.testclient import TestClient

from kiln.config import Settings
from kiln.main import create_app


def make_client() -> TestClient:
    app = create_app(
        Settings(
            _env_file=None,
            model_api_key="spark-key",
            project_token_ceiling=5000,
            pipeline_autostart=False,  # attach without firing a real pipeline
        )
    )
    return TestClient(app)


def test_runs_need_an_idea():
    client = make_client()
    assert client.post("/runs", json={"idea": "  "}).json()["error"]


def test_approval_answer_needs_a_run_and_a_companion():
    client = make_client()
    missing = client.post("/runs/run-nope/approvals", json={"approved": True}).json()
    assert missing["error"] == "unknown run"
    run = client.post("/runs", json={"idea": "a bookstore"}).json()
    lonely = client.post(f"/runs/{run['run_id']}/approvals", json={"approved": True}).json()
    assert lonely["ok"] is False
    assert "no companion" in lonely["error"]


def test_pair_attach_updates_status_and_disconnect_reverts_it():
    client = make_client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    run_id, code = created["run_id"], created["pair_code"]
    assert created["status"] == "waiting_for_companion"
    assert "listen" in created["connect"]
    assert code not in created["connect"]

    with client.websocket_connect("/companion/ws") as ws:
        ws.send_json(
            {"type": "hello", "code": code, "workspace": "D:\\work\\todo", "version": "0.1.0"}
        )
        ack = ws.receive_json()
        assert ack["type"] == "hello_ack"
        assert ack["run_id"] == run_id
        # read the registry directly: a second request does not interleave
        # with an open websocket under TestClient
        run = client.app.state.runs.get_run(run_id)
        assert run.status == "ready"
        assert run.workspace == "D:\\work\\todo"

    assert client.get(f"/runs/{run_id}").json()["status"] == "disconnected"


def test_hello_without_workspace_leaves_the_run_paired():
    client = make_client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    run_id, code = created["run_id"], created["pair_code"]

    with client.websocket_connect("/companion/ws") as ws:
        ws.send_json({"type": "hello", "code": code, "workspace": "", "version": "0.1.0"})
        ack = ws.receive_json()
        assert ack["type"] == "hello_ack"
        run = client.app.state.runs.get_run(run_id)
        assert run.status == "paired"
        assert run.workspace == ""

    assert client.get(f"/runs/{run_id}").json()["status"] == "disconnected"


def test_a_listen_reconnect_mid_run_is_sent_the_locked_folder():
    """A respawned `listen` companion has no folder; the run already locked one.
    The server re-locks it first, or every tool fails "workspace not chosen yet"."""
    client = make_client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    code = created["pair_code"]
    hello = {"type": "hello", "code": code, "workspace": "D:\\work\\todo", "version": "0.1.0"}
    with client.websocket_connect("/companion/ws") as ws:
        ws.send_json(hello)
        assert ws.receive_json()["type"] == "hello_ack"

    with client.websocket_connect("/companion/ws") as ws:
        ws.send_json({**hello, "workspace": ""})
        assert ws.receive_json()["type"] == "hello_ack"
        call = ws.receive_json()
        assert call["type"] == "tool_call"
        assert call["tool"] == "set_workspace"
        assert call["input"] == {"path": "D:\\work\\todo"}
        ws.send_json({"type": "tool_result", "id": call["id"], "ok": True, "output": "{}"})


def test_the_drawer_transcript_includes_an_agents_delegated_work():
    from kiln.agents.transcript import TranscriptLog

    client = make_client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    log = TranscriptLog()
    log.record("worker-1", "worker", "tool_call", {"name": "shell", "args": {}})
    log.record("worker-1/explore-1", "worker", "model_call", {"content": "found it"})
    log.record("worker-10", "worker", "note", {"text": "a different desk"})
    client.app.state.runs.get_run(created["run_id"]).transcript = log

    data = client.get(f"/runs/{created['run_id']}/transcript?agent=worker-1").json()
    assert [e["agent_id"] for e in data["entries"]] == ["worker-1", "worker-1/explore-1"]
    assert "worker-1/explore-1" in data["agents"]


def test_a_bad_pair_code_is_refused():
    client = make_client()
    with client.websocket_connect("/companion/ws") as ws:
        ws.send_json(
            {"type": "hello", "code": "ZZZZZZ", "workspace": "x", "version": "0.1.0"}
        )
        try:
            ws.receive_json()
        except Exception:
            pass  # the server closes the socket after refusing the code


def test_probe_without_a_companion_is_a_clean_error():
    client = make_client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    result = client.post(f"/runs/{created['run_id']}/probe", json={}).json()
    assert result["ok"] is False
    assert "no companion" in result["error"]


def test_notes_queue_against_a_run():
    client = make_client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    run_id = created["run_id"]
    first = client.post(f"/runs/{run_id}/notes", json={"text": "add due dates"}).json()
    assert first == {"status": "queued", "notes": 1}
    second = client.post(f"/runs/{run_id}/notes", json={"text": "and dark mode"}).json()
    assert second["notes"] == 2
    assert client.get(f"/runs/{run_id}").json()["notes"] == 2
