import pytest
from pydantic import ValidationError

from kiln.schemas import Spec, Task, TaskGraph


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
