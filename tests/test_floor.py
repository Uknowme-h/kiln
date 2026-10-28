"""The floor contract: typed events, the roster, and the demo feed."""

from fastapi.testclient import TestClient

from kiln.config import Settings
from kiln.events.demo import DEMO_AGENTS, script_demo_run
from kiln.events.schema import RunStarted, decode_event, encode_event
from kiln.main import create_app


def make_client() -> TestClient:
    app = create_app(
        Settings(
            _env_file=None,
            model_api_key="spark-key",
            project_token_ceiling=5000,
        )
    )
    return TestClient(app)


def test_every_scripted_event_round_trips_through_the_contract():
    for event in script_demo_run():
        decoded = decode_event(encode_event(event))
        assert decoded.kind == event.kind


def test_demo_run_tells_the_full_story_in_order():
    kinds = [event.kind for event in script_demo_run()]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert "task_assigned" in kinds
    assert kinds.count("test_run_finished") == 2  # one red, then green
    assert kinds.count("usage_tick") >= 1


def test_the_demo_rehearses_a_mid_run_client_note():
    events = list(script_demo_run())
    notes = [event for event in events if event.kind == "note_added"]
    assert len(notes) == 1
    assert notes[0].text
    note_index = events.index(notes[0])
    later_kinds = [event.kind for event in events[note_index:]]
    assert "message_sent" in later_kinds  # the planner answers the note
    assert later_kinds[-1] == "run_finished"


def test_demo_run_needs_no_model_or_workspace():
    first = next(script_demo_run())
    assert isinstance(first, RunStarted)
    assert first.idea


def test_roster_endpoint_lists_the_seated_agents():
    client = make_client()
    roster = client.get("/api/agents").json()
    assert len(roster) == len(DEMO_AGENTS)
    assert {agent["role"] for agent in roster} == {
        "requirements",
        "planner",
        "test_author",
        "worker",
        "fixer",
        "reporter",
    }


def test_demo_trigger_broadcasts_to_the_floor():
    client = make_client()
    response = client.post("/demo/run")
    assert response.status_code == 200
    assert response.json()["status"] == "started"


def test_the_demo_run_accepts_the_typed_idea_from_the_brief():
    client = make_client()
    response = client.post("/demo/run", json={"idea": "a bookstore with search"})
    assert response.status_code == 200
    first = next(script_demo_run(idea="a bookstore with search"))
    assert first.idea == "a bookstore with search"


def test_the_note_endpoint_echoes_a_client_note_to_the_floor():
    client = make_client()
    response = client.post("/demo/note", json={"text": "add due dates"})
    assert response.status_code == 200
    assert response.json()["status"] == "noted"
    empty = client.post("/demo/note", json={"text": "   "})
    assert empty.json()["status"] == "ignored"


def test_a_new_demo_run_replaces_one_still_playing():
    client = make_client()
    client.post("/demo/run")
    first = client.app.state.demo_task
    client.post("/demo/run")
    second = client.app.state.demo_task
    assert first is not second
    assert first.cancelled() or first.cancelling() > 0


def test_floor_is_served_at_the_root():
    client = make_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "Kiln" in response.text
