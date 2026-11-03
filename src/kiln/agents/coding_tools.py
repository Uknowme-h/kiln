"""Local tool wrappers for coding agents: explore, todos, skills (F1/F3/F4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from kiln.agents.explore import ExploreQuota, run_explore
from kiln.agents.skills import handle_skill_tool, wrap_call_tool_with_skills
from kiln.agents.todos import TodoStore
from kiln.agents.transcript import TranscriptLog
from kiln.config import Settings
from kiln.gateway.client import Gateway
from kiln.tools.schemas import SpawnExplore, TodoRead, TodoWrite

CallTool = Callable[[str, dict], Awaitable[str]]


def wrap_coding_tools(
    call_tool: CallTool,
    *,
    gateway: Gateway,
    settings: Settings,
    agent_id: str,
    transcript: TranscriptLog | None = None,
    todo_store: TodoStore | None = None,
    explore_quota: ExploreQuota | None = None,
) -> tuple[CallTool, TodoStore, ExploreQuota]:
    """Intercept local tools; delegate the rest (incl. diagnostics) to companion."""
    store = todo_store or TodoStore()
    quota = explore_quota or ExploreQuota(settings.explore_max_per_task)
    explore_index = 0
    base = wrap_call_tool_with_skills(call_tool)

    async def wrapped(name: str, args: dict) -> str:
        nonlocal explore_index
        if name == "skill":
            return handle_skill_tool(args if isinstance(args, dict) else {})
        if name == "todo_write":
            if not settings.enable_todo_tool:
                return "Error: todo tool is disabled"
            try:
                payload = TodoWrite.model_validate(args)
            except Exception as exc:  # noqa: BLE001
                return f"Error: {exc}"
            err = store.write(list(payload.items), merge=bool(payload.merge))
            if err:
                return f"Error: {err}"
            rendered = store.render()
            if transcript is not None:
                transcript.record(
                    agent_id,
                    "worker",
                    "note",
                    {"text": "todos updated", "todos": store.to_json()},
                )
            return "todos updated:\n" + rendered
        if name == "todo_read":
            if not settings.enable_todo_tool:
                return "Error: todo tool is disabled"
            TodoRead.model_validate(args or {})
            return store.render()
        if name == "spawn_explore":
            if not settings.enable_explore_subagent:
                return "Error: explore subagent is disabled"
            try:
                payload = SpawnExplore.model_validate(args)
            except Exception as exc:  # noqa: BLE001
                return f"Error: {exc}"
            refused = quota.try_consume()
            if refused:
                return f"Error: {refused}"
            explore_index += 1
            return await run_explore(
                gateway=gateway,
                call_tool=base,
                prompt=payload.prompt,
                parent_id=agent_id,
                explore_index=explore_index,
                settings=settings,
                transcript=transcript,
            )
        return await base(name, args)

    return wrapped, store, quota
