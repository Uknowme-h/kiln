import pytest

from kiln.agents import planner
from kiln.agents.roles import Role
from kiln.gateway.client import Completion
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import TaskGraph

SPEC_PAYLOAD = {
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
    "assumptions": [],
}

VALID_GRAPH = {
    "spec_title": "Tasks",
    "tasks": [
        {
            "id": "t1",
            "title": "Models",
            "description": "Task storage model",
            "depends_on": [],
            "owned_paths": ["app/models.py"],
        },
        {
            "id": "t2",
            "title": "API",
            "description": "REST endpoints for tasks",
            "depends_on": ["t1"],
            "owned_paths": ["app/routes.py"],
        },
    ],
}


class FakeGateway:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, role, messages, **kwargs):
        self.calls.append({"role": role, "messages": messages, **kwargs})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return Completion(
            content=step, model="muse-spark-1.3", prompt_tokens=1, completion_tokens=1
        )


def _valid_json() -> str:
    import json

    return json.dumps(VALID_GRAPH)


def _spec() -> Spec:
    return Spec.model_validate(SPEC_PAYLOAD)


def test_a_valid_reply_validates_into_a_task_graph():
    gateway = FakeGateway([_valid_json()])
    graph = planner.run(gateway, _spec())
    assert isinstance(graph, TaskGraph)
    assert [task.id for task in graph.tasks] == ["t1", "t2"]
    assert len(gateway.calls) == 1


def test_the_call_uses_the_planner_route_and_structured_output():
    gateway = FakeGateway([_valid_json()])
    planner.run(gateway, _spec())
    call = gateway.calls[0]
    assert call["role"] is Role.PLANNER
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["name"] == "task_graph"


def test_the_spec_is_wrapped_as_untrusted_content_with_criteria_visible():
    gateway = FakeGateway([_valid_json()])
    planner.run(gateway, _spec())
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "<spec>" in user_message and "</spec>" in user_message
    assert "tests/test_tasks.py" in user_message  # oracles reach the planner


def test_user_notes_are_quoted_as_content_too():
    gateway = FakeGateway([_valid_json()])
    planner.run(gateway, _spec(), user_notes=("add due dates",))
    user_message = gateway.calls[0]["messages"][1]["content"]
    assert "<user_notes>\n- add due dates\n</user_notes>" in user_message


def test_an_invalid_reply_is_retried_once_with_the_error_shown():
    import json

    overlapping = dict(VALID_GRAPH)
    overlapping["tasks"] = [
        dict(VALID_GRAPH["tasks"][0]),
        {**VALID_GRAPH["tasks"][1], "owned_paths": ["app/models.py"]},
    ]
    gateway = FakeGateway([json.dumps(overlapping), _valid_json()])
    graph = planner.run(gateway, _spec())
    assert isinstance(graph, TaskGraph)
    assert len(gateway.calls) == 2
    retry_message = gateway.calls[1]["messages"][1]["content"]
    assert "validation_error" in retry_message
    assert "overlapping owned paths" in retry_message  # the real reason is shown


def test_two_invalid_replies_are_counted_as_a_failure():
    gateway = FakeGateway(['{"tasks": []}', '{"nope": true}'])
    with pytest.raises(planner.PlannerError):
        planner.run(gateway, _spec())
    assert len(gateway.calls) == 2
