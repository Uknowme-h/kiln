"""Worker desk seats must stay unique across layers (no overlap with fixer)."""

from __future__ import annotations

import re
from pathlib import Path


def _seat_fn_source() -> str:
    return (Path(__file__).resolve().parents[1] / "ui" / "scene.js").read_text(
        encoding="utf-8"
    )


def test_scene_js_exports_unique_worker_seat_math():
    """Sanity: workerSeat grows instead of modulo-wrapping into fixer desks."""
    src = _seat_fn_source()
    assert "WORKER_ONLY_SEATS" in src
    assert "% WORKER_SEATS.length" not in src
    assert "workerSeat" in src
    # fixer/reviewer keep fixed coords, not shared WORKER_SEATS indices
    assert re.search(r"fixer:\s*\{\s*x:\s*220,\s*y:\s*545", src)
    assert re.search(r"reviewer:\s*\{\s*x:\s*480,\s*y:\s*545", src)


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
