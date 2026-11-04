"""Product briefing blocks injected into worker/fixer prompts (F0).

Design and discovery live on disk; this module turns them into clipped XML
blocks so coding agents honor UX/readiness decisions without an extra tool round.
"""

from __future__ import annotations

import json

from kiln.schemas.spec import Spec
from kiln.schemas.tasks import Task

MAX_DESIGN_CHARS = 6000
MAX_DISCOVERY_CHARS = 3000
MAX_SPEC_EXCERPT_CHARS = 8000
MAX_PLAN_MEMO_CHARS = 4000
MAX_HANDOFF_CHARS = 1500


def clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[{omitted} chars clipped]…\n{text[-tail:]}"


def design_block(design: str | None) -> str:
    body = (design or "").strip()
    if not body:
        return ""
    return f"\n\n<design>\n{clip_text(body, MAX_DESIGN_CHARS)}\n</design>"


def discovery_digest(discovery: str | None) -> str:
    """The whole decisions log, clipped.

    Discovery is free-form (the analyst invents its own topics), so filtering
    it to a fixed set of headings dropped every decision outside that set.
    """
    raw = (discovery or "").strip()
    if not raw:
        return ""
    return f"\n\n<discovery>\n{clip_text(raw, MAX_DISCOVERY_CHARS)}\n</discovery>"


MAX_INVENTORY_ENTRIES = 60


def existing_workspace_block(inventory: list[str] | None) -> str:
    """Announce a non-empty workspace so agents extend it instead of re-specifying it."""
    if not inventory:
        return ""
    listed = "\n".join(f"- {entry}" for entry in inventory[:MAX_INVENTORY_ENTRIES])
    if len(inventory) > MAX_INVENTORY_ENTRIES:
        listed += f"\n- … (+{len(inventory) - MAX_INVENTORY_ENTRIES} more)"
    return (
        "\n\n<existing_workspace>\n"
        "The workspace already contains a project (top level below). Treat the "
        "brief as a change to this project: read what exists before deciding, "
        "keep what it already establishes (stack, layout, conventions) unless "
        "the brief changes it, and build the new or changed behavior — not a "
        "rebuild from scratch.\n"
        f"{listed}\n"
        "</existing_workspace>"
    )


def plan_memo_block(plan: str | None) -> str:
    """The planner's docs/plan.md: shared seams, DOM ids, frozen mounts."""
    body = (plan or "").strip()
    if not body:
        return ""
    return f"\n\n<plan_memo>\n{clip_text(body, MAX_PLAN_MEMO_CHARS)}\n</plan_memo>"


def dependency_handoff_block(handoffs: list[dict] | None) -> str:
    """What each upstream task's worker reported: summary, what works, gaps.

    Paths alone told a dependent worker where the files were, not that the
    seam it builds on was left half-finished.
    """
    if not handoffs:
        return ""
    parts = []
    for handoff in handoffs:
        lines = [f"task {handoff.get('task_id', '?')}: {handoff.get('summary', '').strip()}"]
        if (handoff.get("what_works") or "").strip():
            lines.append(f"what works: {handoff['what_works'].strip()}")
        gaps = [gap for gap in handoff.get("known_gaps") or [] if gap.strip()]
        if gaps:
            lines.append("known gaps:\n" + "\n".join(f"- {gap}" for gap in gaps))
        parts.append(clip_text("\n".join(lines), MAX_HANDOFF_CHARS))
    return (
        "\n\n<dependency_handoff>\n"
        "What the workers of the tasks you depend on reported. Their known gaps "
        "are real: do not assume those seams are finished — read the files, and "
        "record a known_gap if you are blocked by one.\n\n"
        + "\n\n".join(parts)
        + "\n</dependency_handoff>"
    )


def product_briefing_blocks(
    *,
    design: str | None = None,
    discovery: str | None = None,
    prefer_design: bool = False,
) -> str:
    """Compose design + discovery. Under pressure, discovery clips first."""
    design_part = design_block(design)
    discovery_part = discovery_digest(discovery)
    if prefer_design and design_part and discovery_part:
        # Already clipped individually; keep both.
        pass
    return design_part + discovery_part


def spec_excerpt_for_task(spec: Spec, task: Task) -> str:
    """Acceptance criteria + overlapping file_plan for fixer context."""
    covers = set(task.covers or [])
    criteria = [
        c for c in spec.acceptance_criteria if not covers or c.id in covers
    ]
    if not criteria and spec.acceptance_criteria:
        criteria = list(spec.acceptance_criteria)
    owned = task.owned_paths or []

    def overlaps(path: str) -> bool:
        return any(
            path == p or path.startswith(p.rstrip("/") + "/") or p.startswith(path.rstrip("/") + "/")
            for p in owned
        )

    files = [f for f in spec.file_plan if overlaps(f.path)] if owned else list(spec.file_plan)
    payload = {
        "title": spec.title,
        "acceptance_criteria": [c.model_dump() for c in criteria],
        "file_plan": [f.model_dump() for f in files],
    }
    text = json.dumps(payload, indent=2)
    return f"\n\n<spec_excerpt>\n{clip_text(text, MAX_SPEC_EXCERPT_CHARS)}\n</spec_excerpt>"


def briefing_reminder_suffix(*, has_design: bool) -> str:
    if has_design:
        return (
            " Honor <design> tokens/layout and <discovery> product decisions when "
            "present — do not silently downgrade UX or integrations."
        )
    return (
        " Honor <discovery> product decisions when present — do not silently "
        "downgrade auth, persistence, or integrations."
    )
