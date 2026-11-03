"""F0 product briefing digests for workers and fixers."""

from kiln.agents.briefing import (
    design_block,
    discovery_digest,
    product_briefing_blocks,
    spec_excerpt_for_task,
)
from kiln.schemas.spec import AcceptanceCriterion, FilePlanEntry, Spec, UserStory
from kiln.schemas.tasks import Task


def _spec() -> Spec:
    return Spec.model_validate(
        {
            "title": "Notes",
            "summary": "drawing notes",
            "stack_id": "python",
            "stack": "Flask",
            "user_stories": [
                {
                    "id": "us1",
                    "as_a": "user",
                    "i_want": "to draw",
                    "so_that": "I share ideas",
                }
            ],
            "acceptance_criteria": [
                {
                    "id": "ac1",
                    "story_id": "us1",
                    "given": "app",
                    "when": "open",
                    "then": "canvas mounts",
                    "oracle": "tests/ui.test.js checks excalidraw",
                },
                {
                    "id": "ac2",
                    "story_id": "us1",
                    "given": "auth",
                    "when": "login",
                    "then": "session",
                    "oracle": "tests/auth.test.js",
                },
            ],
            "file_plan": [
                {"path": "public/app.js", "purpose": "UI mount"},
                {"path": "server.py", "purpose": "API"},
            ],
            "assumptions": ["demo auth labeled"],
        }
    )


def test_design_block_omitted_when_empty():
    assert design_block("") == ""
    assert design_block(None) == ""


def test_design_and_discovery_injected():
    design = "# Palette\n--accent: coral\n"
    discovery = (
        "## Auth model\n- real sessions\n\n"
        "## Persistence\n- sqlite\n\n"
        "## Integration method\n- official package\n\n"
        "## UI direction\n- editorial\n\n"
        "## Stack choice\n- node\n\n"
        "## Extra noise\n- ignore me\n"
    )
    blocks = product_briefing_blocks(design=design, discovery=discovery)
    assert "<design>" in blocks
    assert "coral" in blocks
    assert "<discovery>" in blocks
    assert "real sessions" in blocks
    assert "Extra noise" not in blocks


def test_discovery_digest_fallback_without_headings():
    digest = discovery_digest("just a paragraph about auth")
    assert "<discovery>" in digest
    assert "just a paragraph" in digest


def test_spec_excerpt_filters_by_covers():
    task = Task.model_validate(
        {
            "id": "t1",
            "title": "UI",
            "kind": "feature",
            "description": "mount drawing",
            "owned_paths": ["public/"],
            "covers": ["ac1"],
            "depends_on": [],
        }
    )
    excerpt = spec_excerpt_for_task(_spec(), task)
    assert "<spec_excerpt>" in excerpt
    assert "ac1" in excerpt
    assert "ac2" not in excerpt
    assert "public/app.js" in excerpt
