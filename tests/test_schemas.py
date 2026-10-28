import pytest
from pydantic import ValidationError

from kiln.schemas import Spec, Task, TaskGraph
from kiln.schemas.jsonschema import strict_json_schema


def _spec(**overrides):
    values = {
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
    values.update(overrides)
    return values


def test_spec_rejects_an_unknown_story_reference():
    payload = _spec()
    payload["acceptance_criteria"][0]["story_id"] = "missing"
    with pytest.raises(ValidationError):
        Spec.model_validate(payload)


def test_every_criterion_needs_a_named_oracle():
    payload = _spec()
    payload["acceptance_criteria"][0]["oracle"] = ""
    with pytest.raises(ValidationError):
        Spec.model_validate(payload)
    del payload["acceptance_criteria"][0]["oracle"]
    with pytest.raises(ValidationError):
        Spec.model_validate(payload)


def test_strict_schema_closes_objects_and_requires_every_property():
    schema = strict_json_schema(Spec)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    criterion = schema["$defs"]["AcceptanceCriterion"]
    assert criterion["additionalProperties"] is False
    assert set(criterion["required"]) == set(criterion["properties"])
    assert "oracle" in criterion["required"]


def test_task_graph_rejects_overlapping_paths_and_cycles():
    with pytest.raises(ValidationError):
        TaskGraph.model_validate(
            {
                "spec_title": "Tasks",
                "tasks": [
                    Task(id="a", title="A", description="one", owned_paths=["src"]),
                    Task(id="b", title="B", description="two", owned_paths=["src/app.py"]),
                ],
            }
        )
    with pytest.raises(ValidationError):
        TaskGraph.model_validate(
            {
                "spec_title": "Tasks",
                "tasks": [
                    {
                        "id": "a",
                        "title": "A",
                        "description": "one",
                        "depends_on": ["b"],
                        "owned_paths": ["a.py"],
                    },
                    {
                        "id": "b",
                        "title": "B",
                        "description": "two",
                        "depends_on": ["a"],
                        "owned_paths": ["b.py"],
                    },
                ],
            }
        )


def test_owned_paths_cannot_escape_the_workspace():
    with pytest.raises(ValidationError):
        Task(id="a", title="A", description="one", owned_paths=["../secret"])
