"""Product briefing blocks injected into worker/fixer prompts (F0).

Design and discovery live on disk; this module turns them into clipped XML
blocks so coding agents honor UX/readiness decisions without an extra tool round.
"""

from __future__ import annotations

import re

from kiln.schemas.spec import Spec
from kiln.schemas.tasks import Task

MAX_DESIGN_CHARS = 6000
MAX_DISCOVERY_CHARS = 3000
MAX_SPEC_EXCERPT_CHARS = 8000

_READINESS_TITLES = (
    "Auth model",
    "Persistence",
    "Integration method",
    "UI direction",
    "Stack choice",
)


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
    """Prefer known readiness headings when present; else free-form clip."""
    raw = (discovery or "").strip()
    if not raw:
        return ""
    parts: list[str] = []
    for title in _READINESS_TITLES:
        pattern = re.compile(
            rf"(##\s*{re.escape(title)}\b.*?)(?=\n##\s|\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(raw)
        if match:
            parts.append(match.group(1).strip())
    if not parts:
        # Free-form decisions log (agent-driven clarify) — clip the whole file.
        parts = [raw]
    digest = "\n\n".join(parts)
    return f"\n\n<discovery>\n{clip_text(digest, MAX_DISCOVERY_CHARS)}\n</discovery>"


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
    import json

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
