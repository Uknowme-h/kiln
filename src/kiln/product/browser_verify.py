"""Host-only Playwright click-through against a FlowPlan."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

from kiln.product.browser_flows import resolve_flow_plan
from kiln.product.dev_server import start_dev_server
from kiln.schemas.browser_flow import Flow, FlowPlan, FlowStep


def _locator(page, step: FlowStep):
    if step.selector:
        return page.locator(step.selector).first
    if step.role and step.name:
        return page.get_by_role(step.role, name=re.compile(re.escape(step.name), re.I))
    if step.text:
        return page.get_by_text(step.text, exact=False).first
    raise ValueError(f"no locator for step {step.kind}")


def _run_step(page, step: FlowStep, base_url: str, console_errors: list[str]) -> None:
    timeout = step.timeout_ms
    if step.kind == "goto":
        raw = step.url or "/"
        if raw.startswith("http://") or raw.startswith("https://"):
            target = raw
        else:
            target = urljoin(base_url.rstrip("/") + "/", raw.lstrip("/"))
        page.goto(target, wait_until="domcontentloaded", timeout=timeout)
        return
    if step.kind == "click":
        _locator(page, step).click(timeout=timeout)
        return
    if step.kind == "fill":
        _locator(page, step).fill(step.value or "", timeout=timeout)
        return
    if step.kind == "expect_visible":
        _locator(page, step).wait_for(state="visible", timeout=timeout)
        return
    if step.kind == "expect_text":
        page.get_by_text(step.text or "", exact=False).first.wait_for(
            state="visible", timeout=timeout
        )
        return
    if step.kind == "expect_url":
        page.wait_for_url(f"**{step.url}**", timeout=timeout)
        return
    if step.kind == "expect_console_clean":
        if console_errors:
            raise AssertionError(
                "console errors: " + "; ".join(console_errors[:5])
            )
        return
    raise ValueError(f"unknown step kind {step.kind}")


def _run_flow(page, flow: Flow, base_url: str, console_errors: list[str]) -> None:
    for step in flow.steps:
        _run_step(page, step, base_url, console_errors)


def _browser_missing(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "executable doesn't exist" in msg or "playwright install" in msg


def run_browser_verify(
    root: Path,
    *,
    idea: str = "",
    discovery: str = "",
    base_url: str | None = None,
    plan: FlowPlan | None = None,
    timeout: float = 20.0,
) -> list[str]:
    """Execute FlowPlan in headless Chromium; return gap strings (empty = pass)."""
    root = Path(root)
    gaps: list[str] = []
    server = None
    url = (base_url or "").rstrip("/")
    shot_dir = root / ".kiln" / "browser"
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            # Host tooling missing — advisory skip (same as no start command).
            # Do not return a gap: product-gate gaps force the fixer and can
            # abort an otherwise green run over something code cannot fix.
            return []

        if not url:
            server, start_gap = start_dev_server(root, timeout=timeout)
            if start_gap:
                return [f"browser_verify: {start_gap}"]
            if server is None:
                return []  # no start command — advisory skip, same as smoke
            url = server.base_url

        flow_plan = plan or resolve_flow_plan(root, idea=idea, discovery=discovery)
        flow_plan = flow_plan.model_copy(update={"base_url": url})

        shot_dir.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True)
                except Exception as exc:  # noqa: BLE001
                    if _browser_missing(exc):
                        # Browsers not installed on this host — advisory skip.
                        return []
                    return [f"browser_verify: failed to launch chromium: {exc}"]
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    console_errors: list[str] = []

                    def on_console(msg) -> None:
                        if msg.type == "error":
                            console_errors.append(msg.text)

                    page.on("console", on_console)
                    for flow in flow_plan.flows:
                        console_errors.clear()
                        try:
                            _run_flow(page, flow, url, console_errors)
                        except Exception as exc:  # noqa: BLE001
                            shot = shot_dir / f"{flow.id}.png"
                            try:
                                page.screenshot(path=str(shot), full_page=True)
                                shot_note = f" (screenshot {shot.as_posix()})"
                            except Exception:  # noqa: BLE001
                                shot_note = ""
                            gaps.append(
                                f"flow {flow.id}: {exc}{shot_note}".replace("\n", " ")
                            )
                    context.close()
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            if _browser_missing(exc):
                return []
            raise
    finally:
        if server is not None:
            server.stop()
    return gaps
