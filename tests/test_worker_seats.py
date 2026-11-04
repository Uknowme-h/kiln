"""Worker desk seats must stay unique across layers (no overlap with fixer)."""

from __future__ import annotations

import re
from pathlib import Path


def _seat_fn_source() -> str:
    return (Path(__file__).resolve().parents[1] / "ui" / "scene.js").read_text(
        encoding="utf-8"
    )


def test_scene_js_keeps_workers_off_the_fixed_desks():
    """Workers hot-desk from their own pool; the fixer and reviewer keep fixed
    seats that no worker can be given, and nothing wraps by modulo."""
    src = _seat_fn_source()
    fixed = re.search(r"const FIXED_SEATS = \{(.*?)\};", src, re.S)
    build = re.search(r"const BUILD_SEATS = \[(.*?)\];", src, re.S)
    assert fixed and build
    for role in ("fixer", "reviewer", "requirements", "planner", "test_author", "reporter"):
        assert re.search(rf"\b{role}: seat\(", fixed.group(1)), role
    assert "fixer" not in build.group(1) and "reviewer" not in build.group(1)
    # the six build desks are distinct grid cells
    cells = re.findall(r'seat\("b\d", (COL\.\w+), (ROW\.\w+)', build.group(1))
    assert len(cells) == 6 and len(set(cells)) == 6
    # a worker's seat comes from the build pool (or the table overflow), never modulo
    assert "BUILD_SEATS.find" in src
    assert "% BUILD_SEATS.length" not in src


def test_monotonic_worker_ids_in_orchestrator():
    src = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "kiln"
        / "graph"
        / "orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "layer_index * 4" not in src
    assert "_worker_seq" in src
