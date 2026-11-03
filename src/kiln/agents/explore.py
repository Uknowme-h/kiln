"""Read-only explore subagent (F1) — OpenCode Explore pattern for Kiln workers."""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable

from kiln.agents.loop import AgentLoopError, run_agent_loop
from kiln.agents.roles import Role
from kiln.agents.transcript import TranscriptLog
from kiln.config import Settings
from kiln.gateway.client import Gateway
from kiln.tools.registry import TOOLS, api_tool
from kiln.tools.schemas import SubmitExplore

CallTool = Callable[[str, dict], Awaitable[str]]

_EXPLORE_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "kiln_explore_depth", default=0
)

EXPLORE_TOOLS = (
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "web_search",
    "web_fetch",
    "submit_explore",
)

EXPLORE_SYSTEM = (
    "You are a read-only explore subagent. Search and read the workspace (and "
    "the web if needed) to answer the parent developer's question. You cannot "
    "edit files, run shell, or spawn further explores. When done, call "
    "submit_explore with a concise summary, paths you touched, and any open "
    "questions. Prefer paths and short quotes over dumping whole files."
)

MAX_SUMMARY_CHARS = 4000


class ExploreQuota:
    """Per-parent-task explore budget."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def try_consume(self) -> str | None:
        if self.used >= self.limit:
            return (
                f"explore budget exhausted ({self.limit} per task) — "
                "use prior summaries or read_file/glob/grep directly"
            )
        self.used += 1
        return None


def explore_tool_specs() -> list[dict]:
    return [api_tool(TOOLS[name]) for name in EXPLORE_TOOLS]


async def run_explore(
    *,
    gateway: Gateway,
    call_tool: CallTool,
    prompt: str,
    parent_id: str,
    explore_index: int,
    settings: Settings,
    transcript: TranscriptLog | None = None,
) -> str:
    depth = _EXPLORE_DEPTH.get()
    if depth >= 1:
        return "Error: explore depth limit reached (1) — cannot nest explore inside explore"

    token = _EXPLORE_DEPTH.set(depth + 1)
    child_id = f"{parent_id}/explore-{explore_index}"
    try:
        # Explore may only use read/search tools — filter call_tool.
        allowed = set(EXPLORE_TOOLS) - {"submit_explore"}

        async def child_call(name: str, args: dict) -> str:
            if name not in allowed:
                return f"Error: explore cannot call {name!r}"
            return await call_tool(name, args)

        user = (
            "Answer this research question for the parent developer. "
            "Call submit_explore when done.\n\n"
            f"<explore_question>\n{prompt.strip()}\n</explore_question>"
        )
        try:
            outcome = await run_agent_loop(
                gateway=gateway,
                role=Role.WORKER,
                system=EXPLORE_SYSTEM,
                user=user,
                tools=explore_tool_specs(),
                call_tool=child_call,
                finish=SubmitExplore,
                max_steps=settings.explore_max_steps,
                finish_name="submit_explore",
                transcript=transcript,
                agent_id=child_id,
            )
        except AgentLoopError as exc:
            return f"explore failed: {exc}"

        summary = (outcome.result.summary or "").strip()
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[: MAX_SUMMARY_CHARS - 20] + "\n…[summary clipped]"
        paths = outcome.result.paths_touched or []
        questions = outcome.result.open_questions or []
        lines = [summary]
        if paths:
            lines.append("paths: " + ", ".join(paths[:30]))
        if questions:
            lines.append("open_questions: " + "; ".join(questions[:8]))
        return "\n".join(lines)
    finally:
        _EXPLORE_DEPTH.reset(token)
