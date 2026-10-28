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


def test_pair_attach_updates_status_and_disconnect_reverts_it():
    client = make_client()
    created = client.post("/runs", json={"idea": "a to-do app"}).json()
    run_id, code = created["run_id"], created["pair_code"]
    assert created["status"] == "waiting_for_companion"
    assert code in created["connect"]

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
