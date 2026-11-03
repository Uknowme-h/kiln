"""Load or heuristically generate a browser FlowPlan for UI product gates."""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiln.schemas.browser_flow import BROWSER_FLOWS_DOC, Flow, FlowPlan, FlowStep


def load_flow_plan(root: Path) -> FlowPlan | None:
    path = Path(root) / BROWSER_FLOWS_DOC
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return FlowPlan.model_validate(data)
    except Exception:  # noqa: BLE001 — treat bad plans as missing; fallback wins
        return None


def write_flow_plan(root: Path, plan: FlowPlan) -> Path:
    path = Path(root) / BROWSER_FLOWS_DOC
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def _mentions(blob: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", blob, re.IGNORECASE) for w in words)


def fallback_flow_plan(*, idea: str = "", discovery: str = "") -> FlowPlan:
    """Happy-path flows from brief heuristics when docs/browser-flows.json is missing."""
    blob = f"{idea}\n{discovery}"
    flows: list[Flow] = [
        Flow(
            id="landing",
            name="Landing loads",
            steps=[
                FlowStep(kind="goto", url="/"),
                FlowStep(kind="expect_visible", selector="body"),
            ],
        )
    ]
    if _mentions(blob, "sign up", "signup", "register", "create account"):
        flows.append(
            Flow(
                id="signup",
                name="Signup journey",
                steps=[
                    FlowStep(kind="goto", url="/"),
                    FlowStep(
                        kind="click",
                        role="link",
                        name="Sign up",
                        selector='a[href*="sign"], a[href*="register"], button:has-text("Sign")',
                    ),
                    FlowStep(
                        kind="fill",
                        selector='input[type="email"], input[name*="email" i]',
                        value="kiln-user@example.com",
                    ),
                    FlowStep(
                        kind="fill",
                        selector='input[type="password"]',
                        value="kiln-test-pass-1",
                    ),
                    FlowStep(
                        kind="click",
                        role="button",
                        name="Sign up",
                        selector='button[type="submit"], button:has-text("Sign")',
                    ),
                ],
            )
        )
    if _mentions(blob, "login", "sign in", "signin", "log in"):
        flows.append(
            Flow(
                id="login",
                name="Login journey",
                steps=[
                    FlowStep(kind="goto", url="/"),
                    FlowStep(
                        kind="click",
                        role="link",
                        name="Log in",
                        selector='a[href*="login"], a[href*="sign-in"], button:has-text("Log")',
                    ),
                    FlowStep(
                        kind="fill",
                        selector='input[type="email"], input[name*="email" i]',
                        value="kiln-user@example.com",
                    ),
                    FlowStep(
                        kind="fill",
                        selector='input[type="password"]',
                        value="kiln-test-pass-1",
                    ),
                    FlowStep(
                        kind="click",
                        role="button",
                        name="Log in",
                        selector='button[type="submit"], button:has-text("Log")',
                    ),
                ],
            )
        )
    if _mentions(blob, "excalidraw", "canvas", "whiteboard", "drawing"):
        flows.append(
            Flow(
                id="drawing-mount",
                name="Drawing surface mounted",
                steps=[
                    FlowStep(kind="goto", url="/"),
                    FlowStep(
                        kind="expect_visible",
                        selector=(
                            "canvas, .excalidraw, [class*='excalidraw'], "
                            "#drawing-host canvas, [data-testid='excalidraw']"
                        ),
                    ),
                ],
            )
        )
    # One core interactive control when UI is implied but no specific journey.
    if len(flows) == 1 and _mentions(
        blob, "ui", "page", "frontend", "react", "dashboard", "app"
    ):
        flows.append(
            Flow(
                id="primary-cta",
                name="Primary control visible",
                steps=[
                    FlowStep(kind="goto", url="/"),
                    FlowStep(
                        kind="expect_visible",
                        selector="button, a[href], input, [role='button']",
                    ),
                ],
            )
        )
    return FlowPlan(flows=flows)


def resolve_flow_plan(root: Path, *, idea: str = "", discovery: str = "") -> FlowPlan:
    return load_flow_plan(root) or fallback_flow_plan(idea=idea, discovery=discovery)
