import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kiln.config import Settings
from kiln.events.log import EventLog
from kiln.graph.pipeline import build_pipeline
from kiln.graph.state import NODE_ORDER
from kiln.main import create_app
from kiln.schemas import Spec, TaskGraph, TestSuite
from kiln.tools import ReadFile, WriteFile


def test_health_routing_and_usage_hide_the_api_key():
    app = create_app(
        Settings(
            _env_file=None,
            model_api_key="spark-key",
            spark_model="muse-spark-1.3",
            project_token_ceiling=5000,
        )
    )
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    routing = client.get("/routing").json()
    assert routing["requirements"]["model"] == "muse-spark-1.3"
    assert routing["worker"]["fallback"] is True
    assert "spark-key" not in str(routing)

    usage = client.get("/usage").json()
    assert usage["calls"] == 0
    assert usage["budget"]["ceiling"] == 5000


def test_pipeline_walks_every_phase_and_records_events():
    events = EventLog()
    pipeline = build_pipeline(events)
    result = pipeline.invoke({"idea": "a task list"})
    assert result["phase"] == "reporter"
    assert [event.kind for event in events.list()] == list(NODE_ORDER)


def test_the_requirements_node_calls_the_analyst_and_stores_the_spec():
    seen = {}

    def fake_analyst(idea, user_notes):
        seen["idea"] = idea
        seen["user_notes"] = user_notes
        return Spec.model_validate(_spec_payload())

    pipeline = build_pipeline(analyst=fake_analyst)
    result = pipeline.invoke({"idea": "a task list", "user_notes": ["add due dates"]})
    assert result["spec"].title == "Tasks"
    assert seen == {"idea": "a task list", "user_notes": ("add due dates",)}


def test_the_planner_node_turns_the_spec_into_a_task_graph():
    def fake_analyst(idea, user_notes):
        return Spec.model_validate(_spec_payload())

    def fake_planner(spec, user_notes):
        assert spec.title == "Tasks"
        return TaskGraph.model_validate(
            {
                "spec_title": spec.title,
                "tasks": [
                    {
                        "id": "t1",
                        "title": "API",
                        "description": "endpoints",
                        "owned_paths": ["app/routes.py"],
                    }
                ],
            }
        )

    pipeline = build_pipeline(analyst=fake_analyst, planner=fake_planner)
    result = pipeline.invoke({"idea": "a task list"})
    assert result["tasks"].spec_title == "Tasks"
    assert result["tasks"].tasks[0].owned_paths == ["app/routes.py"]


def test_the_planner_node_needs_a_spec_first():
    # planner is wired but requirements is stubbed, so no spec exists
    graph = build_pipeline(planner=lambda spec, notes: None)
    with pytest.raises(Exception):
        graph.invoke({"idea": "x"})


def test_the_test_author_node_turns_the_spec_into_a_suite():
    def fake_analyst(idea, user_notes):
        return Spec.model_validate(_spec_payload())

    def fake_author(spec):
        assert spec.acceptance_criteria[0].id == "c1"
        return TestSuite.model_validate(
            {
                "spec_title": spec.title,
                "files": [
                    {
                        "path": "tests/test_tasks.py",
                        "content": "def test_x():\n    ...\n",
                        "covers": ["c1"],
                    }
                ],
            },
            context={"criterion_ids": ["c1"]},
        )

    pipeline = build_pipeline(analyst=fake_analyst, test_author=fake_author)
    result = pipeline.invoke({"idea": "a task list"})
    assert result["tests"].files[0].path == "tests/test_tasks.py"


def _spec_payload() -> dict:
    return {
        "title": "Tasks",
        "summary": "A small task list.",
        "stack": "Python",
        "user_stories": [
            {"id": "s1", "as_a": "user", "i_want": "to add a task", "so_that": "I remember it"}
        ],
        "acceptance_criteria": [
            {
                "id": "c1",
                "story_id": "s1",
                "given": "a list",
                "when": "I add one",
                "then": "it is stored",
                "oracle": "tests/test_tasks.py — adding a task persists it",
            }
        ],
        "file_plan": [{"path": "app.py", "purpose": "the app"}],
    }


def test_file_tools_reject_paths_outside_the_workspace():
    assert WriteFile(path="src/app.py", content="print(1)").path == "src/app.py"
    with pytest.raises(ValidationError):
        ReadFile(path="/etc/passwd")
