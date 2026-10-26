import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kiln.config import Settings
from kiln.events.log import EventLog
from kiln.graph.pipeline import build_pipeline
from kiln.graph.state import NODE_ORDER
from kiln.main import create_app
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


def test_file_tools_reject_paths_outside_the_workspace():
    assert WriteFile(path="src/app.py", content="print(1)").path == "src/app.py"
    with pytest.raises(ValidationError):
        ReadFile(path="/etc/passwd")
