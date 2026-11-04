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


# ── multi-root frameworks / roots ────────────────────────────────────────────


def test_spec_accepts_a_split_api_plus_spa():
    spec = Spec.model_validate(
        _spec(
            stack_id="python",
            frameworks=["fastapi", "vite_react"],
            roots=[
                {"path": "backend", "role": "api", "framework": "fastapi"},
                {"path": "frontend", "role": "web", "framework": "vite_react"},
            ],
        )
    )
    assert [root.path for root in spec.roots] == ["backend", "frontend"]


def test_spec_requires_split_roots_for_api_plus_vite():
    with pytest.raises(ValidationError, match="split roots"):
        Spec.model_validate(
            _spec(
                frameworks=["fastapi", "vite_react"],
                roots=[{"path": ".", "role": "fullstack", "framework": "fastapi"}],
            )
        )


def test_spec_rejects_a_root_naming_an_undeclared_framework():
    with pytest.raises(ValidationError, match="not in spec.frameworks"):
        Spec.model_validate(
            _spec(
                frameworks=["fastapi"],
                roots=[{"path": "web", "role": "web", "framework": "vite_react"}],
            )
        )


def test_nextjs_plus_api_forces_node_stack():
    with pytest.raises(ValidationError, match="stack_id 'node'"):
        Spec.model_validate(
            _spec(
                stack_id="python",
                frameworks=["nextjs", "fastapi"],
                roots=[
                    {"path": "web", "role": "fullstack", "framework": "nextjs"},
                    {"path": "api", "role": "api", "framework": "fastapi"},
                ],
            )
        )
    ok = Spec.model_validate(
        _spec(
            stack_id="node",
            frameworks=["nextjs", "fastapi"],
            roots=[
                {"path": "web", "role": "fullstack", "framework": "nextjs"},
                {"path": "api", "role": "api", "framework": "fastapi"},
            ],
        )
    )
    assert ok.stack_id == "node"


def test_nextjs_alone_may_be_a_single_root():
    spec = Spec.model_validate(
        _spec(
            stack_id="node",
            frameworks=["nextjs"],
            roots=[{"path": ".", "role": "fullstack", "framework": "nextjs"}],
        )
    )
    assert spec.roots[0].framework == "nextjs"


def test_overlapping_roots_are_rejected():
    with pytest.raises(ValidationError, match="overlapping roots"):
        Spec.model_validate(
            _spec(
                frameworks=["fastapi", "vite_react"],
                roots=[
                    {"path": "app", "role": "api", "framework": "fastapi"},
                    {"path": "app/web", "role": "web", "framework": "vite_react"},
                ],
            )
        )


def test_root_relative_owned_paths_are_made_workspace_relative():
    task = Task(
        id="f", title="Auth UI", description="x", root="frontend",
        owned_paths=["src/pages/Auth.jsx", "frontend/src/lib/api.js"],
    )
    assert task.owned_paths == ["frontend/src/pages/Auth.jsx", "frontend/src/lib/api.js"]
    assert Task(id="s", title="s", description="x", root=".", owned_paths=["app.py"]).root == ""


def _run_7f74e69c_graph():
    """The task graph that let scaffolds swallow every feature's files."""
    def t(tid, kind, root, owned):
        return {"id": tid, "title": tid, "description": "x", "kind": kind, "root": root, "owned_paths": owned}

    return {
        "spec_title": "Inkboard",
        "tasks": [
            t("S-BE", "scaffold", "backend", ["backend"]),
            t("S-FE", "scaffold", "frontend", ["frontend"]),
            t("F-AUTH", "feature", "backend", ["app/auth.py", "app/routers_auth.py"]),
            t("F-WHITE", "feature", "frontend", ["src/components/Whiteboard.jsx"]),
            t("F-AUTHUI", "feature", "frontend", ["src/lib/api.js", "src/pages/Auth.jsx"]),
        ],
    }


def test_a_scaffold_owning_its_whole_root_over_features_is_rejected():
    with pytest.raises(ValidationError, match="owns the whole root 'backend'.*seam files"):
        TaskGraph.model_validate(_run_7f74e69c_graph())


def test_a_multi_root_graph_with_seam_scaffolds_is_accepted():
    graph = _run_7f74e69c_graph()
    graph["tasks"][0]["owned_paths"] = ["app/main.py", "app/db.py", "requirements.txt"]
    graph["tasks"][1]["owned_paths"] = ["src/App.tsx", "src/main.tsx", "package.json"]
    accepted = TaskGraph.model_validate(graph)
    by_id = {task.id: task for task in accepted.tasks}
    assert by_id["S-BE"].owned_paths == ["backend/app/main.py", "backend/app/db.py", "backend/requirements.txt"]
    assert by_id["F-AUTHUI"].owned_paths == ["frontend/src/lib/api.js", "frontend/src/pages/Auth.jsx"]


def test_required_skills_must_be_built_in_catalog_names():
    with pytest.raises(ValidationError, match="unknown required skill"):
        TaskGraph.model_validate(
            {
                "spec_title": "Tasks",
                "tasks": [
                    {
                        "id": "a",
                        "title": "A",
                        "description": "one",
                        "owned_paths": ["app.py"],
                        "required_skills": ["not_a_skill"],
                    }
                ],
            }
        )
    # a project: overlay can never satisfy a requirement
    with pytest.raises(ValidationError, match="unknown required skill"):
        TaskGraph.model_validate(
            {
                "spec_title": "Tasks",
                "tasks": [
                    {
                        "id": "a",
                        "title": "A",
                        "description": "one",
                        "owned_paths": ["app.py"],
                        "required_skills": ["project:foo"],
                    }
                ],
            }
        )


def test_at_most_one_scaffold_per_root():
    # two scaffolds for disjoint roots is allowed (multi-root product)
    ok = TaskGraph.model_validate(
        {
            "spec_title": "Tasks",
            "tasks": [
                {"id": "sb", "title": "B", "description": "x", "kind": "scaffold", "root": "backend", "owned_paths": ["backend"]},
                {"id": "sf", "title": "F", "description": "y", "kind": "scaffold", "root": "frontend", "owned_paths": ["frontend"]},
            ],
        }
    )
    assert len([t for t in ok.tasks if t.kind == "scaffold"]) == 2
    # but two scaffolds for the SAME root is rejected (non-overlapping paths,
    # so the per-root rule — not the overlap rule — is what fires)
    with pytest.raises(ValidationError, match="multiple scaffold tasks"):
        TaskGraph.model_validate(
            {
                "spec_title": "Tasks",
                "tasks": [
                    {"id": "s1", "title": "A", "description": "x", "kind": "scaffold", "root": "backend", "owned_paths": ["backend/app"]},
                    {"id": "s2", "title": "B", "description": "y", "kind": "scaffold", "root": "backend", "owned_paths": ["backend/tests_helper"]},
                ],
            }
        )
