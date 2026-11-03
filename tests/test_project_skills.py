"""Project skills under .kiln/skills (F4)."""

from pathlib import Path

from kiln.agents.skills import (
    clear_project_skills,
    handle_skill_tool,
    list_skills,
    register_project_skill_texts,
    register_project_skills,
)


def test_register_project_skills_from_disk(tmp_path: Path):
    clear_project_skills()
    skills = tmp_path / ".kiln" / "skills"
    skills.mkdir(parents=True)
    (skills / "house.md").write_text("# House style\nUse serif headlines.\n", encoding="utf-8")
    agents = tmp_path / ".kiln" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("# Reviewer notes\nPrefer short PRs.\n", encoding="utf-8")
    n = register_project_skills(tmp_path, enabled=True)
    assert n == 2
    names = {s["name"] for s in list_skills()}
    assert "project:house" in names
    assert "project:agent-reviewer" in names
    assert "ui_product" in names
    body = handle_skill_tool({"name": "project:house"})
    assert "serif" in body
    assert "untrusted" in body.lower()
    clear_project_skills()


def test_register_project_skill_texts_remote():
    clear_project_skills()
    n = register_project_skill_texts(
        {".kiln/skills/api.md": "# API notes\nPrefer REST.\n"},
        enabled=True,
    )
    assert n == 1
    assert handle_skill_tool({"name": "project:api"}).startswith("Project skill")
    clear_project_skills()
