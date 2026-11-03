"""Host browser_verify against a static HTML fixture (no live LLM)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kiln.product.browser_flows import fallback_flow_plan, resolve_flow_plan, write_flow_plan
from kiln.product.browser_verify import run_browser_verify
from kiln.schemas.browser_flow import Flow, FlowPlan, FlowStep

pytest.importorskip("playwright")


@contextmanager
def serve_directory(directory: Path):
    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


@pytest.fixture
def static_site(tmp_path: Path):
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body>"
        "<h1>Dashboard</h1>"
        "<canvas id='board'></canvas>"
        "</body></html>",
        encoding="utf-8",
    )
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "index.html").write_text(
        "<!doctype html><html><body>"
        "<h1>Notes</h1>"
        "<textarea id='drawing-host'></textarea>"
        "</body></html>",
        encoding="utf-8",
    )
    return tmp_path


def test_fallback_flow_includes_drawing_when_brief_mentions_excalidraw():
    plan = fallback_flow_plan(idea="notes with Excalidraw canvas")
    ids = {f.id for f in plan.flows}
    assert "landing" in ids
    assert "drawing-mount" in ids


def test_resolve_prefers_workspace_file(tmp_path: Path):
    custom = FlowPlan(
        flows=[
            Flow(
                id="only",
                name="Only",
                steps=[
                    FlowStep(kind="goto", url="/"),
                    FlowStep(kind="expect_visible", selector="body"),
                ],
            )
        ]
    )
    write_flow_plan(tmp_path, custom)
    loaded = resolve_flow_plan(tmp_path, idea="excalidraw signup")
    assert len(loaded.flows) == 1
    assert loaded.flows[0].id == "only"


def _skip_if_no_browser(gaps: list[str]) -> None:
    if gaps and any("playwright browsers missing" in g for g in gaps):
        pytest.skip("playwright chromium not installed (run: playwright install chromium)")


def test_browser_verify_passes_when_canvas_mounted(static_site: Path):
    plan = FlowPlan(
        flows=[
            Flow(
                id="drawing-mount",
                name="Canvas",
                steps=[
                    FlowStep(kind="goto", url="/"),
                    FlowStep(kind="expect_text", text="Dashboard"),
                    FlowStep(kind="expect_visible", selector="canvas"),
                ],
            )
        ]
    )
    with serve_directory(static_site) as base_url:
        gaps = run_browser_verify(static_site, base_url=base_url, plan=plan)
    _skip_if_no_browser(gaps)
    assert gaps == []


def test_browser_verify_fails_on_textarea_stub(static_site: Path):
    plan = FlowPlan(
        flows=[
            Flow(
                id="drawing-mount",
                name="Canvas",
                steps=[
                    FlowStep(kind="goto", url="/"),
                    FlowStep(kind="expect_visible", selector="canvas"),
                ],
            )
        ]
    )
    with serve_directory(static_site / "stub") as base_url:
        gaps = run_browser_verify(static_site / "stub", base_url=base_url, plan=plan)
    _skip_if_no_browser(gaps)
    assert gaps
    assert "drawing-mount" in gaps[0]


def test_browser_missing_helper_detects_install_errors():
    from kiln.product.browser_verify import _browser_missing

    assert _browser_missing(RuntimeError("Executable doesn't exist at /tmp/chrome"))
    assert _browser_missing(RuntimeError("Please run: playwright install chromium"))
    assert not _browser_missing(RuntimeError("timeout waiting for selector"))
