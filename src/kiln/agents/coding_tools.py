"""Local tool wrappers for coding agents: explore, todos, skills, peer notes (F1/F3/F4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from kiln.agents.explore import ExploreQuota, run_explore
from kiln.agents.skills import handle_skill_tool, wrap_call_tool_with_skills
from kiln.agents.todos import TodoStore
from kiln.agents.transcript import TranscriptLog
from kiln.config import Settings
from kiln.gateway.client import Gateway
from kiln.tools.schemas import (
    AskPeer,
    Escalate,
    NotifyPeer,
    ProposeContractChange,
    RequestFiles,
    SpawnExplore,
    TodoRead,
    TodoWrite,
)

CallTool = Callable[[str, dict], Awaitable[str]]
OnNotify = Callable[..., Awaitable[str]]  # (message, reply_to="")
OnRequestFiles = Callable[[list[str], str, bool], Awaitable[str]]
OnAsk = Callable[[str, bool], Awaitable[str]]
OnPropose = Callable[[ProposeContractChange], Awaitable[str]]
OnEscalate = Callable[[Escalate], Awaitable[str]]

WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def wrap_coding_tools(
    call_tool: CallTool,
    *,
    gateway: Gateway,
    settings: Settings,
    agent_id: str,
    transcript: TranscriptLog | None = None,
    todo_store: TodoStore | None = None,
    explore_quota: ExploreQuota | None = None,
    on_notify: OnNotify | None = None,
    on_request_files: OnRequestFiles | None = None,
    on_ask: OnAsk | None = None,
    on_propose: OnPropose | None = None,
    on_escalate: OnEscalate | None = None,
    required_skills: tuple[str, ...] = (),
) -> tuple[CallTool, TodoStore, ExploreQuota]:
    """Intercept local tools; delegate the rest (incl. diagnostics) to the companion.

    ``on_notify`` delivers ``notify_peer`` to the teammate (absent: no team).
    ``required_skills`` are the task's must-have packs. They are embedded in
    the system prompt (``system_prompt_for``), so a ``skill`` call for one is
    answered with a pointer instead of a second copy.
    """
    store = todo_store or TodoStore()
    quota = explore_quota or ExploreQuota(settings.explore_max_per_task)
    explore_index = 0
    base = wrap_call_tool_with_skills(call_tool)

    async def wrapped(name: str, args: dict) -> str:
        nonlocal explore_index
        if name == "skill":
            skill_name = str((args or {}).get("name") or "").strip()
            if skill_name in required_skills:
                return (
                    f"the {skill_name} skill is already in your instructions (Required skills "
                    "section of the system prompt) — follow it from there"
                )
            return handle_skill_tool(args if isinstance(args, dict) else {})
        if name == "notify_peer":
            if on_notify is None:
                return "Error: no teammate on this run — record the note in known_gaps instead"
            try:
                payload = NotifyPeer.model_validate(args)
            except Exception as exc:  # noqa: BLE001
                return f"Error: {exc}"
            if payload.reply_to:
                return await on_notify(payload.message, payload.reply_to)
            return await on_notify(payload.message)
        if name == "ask_peer":
            if on_ask is None:
                return "Error: no teammate on this run — read the contract and their files instead"
            try:
                payload = AskPeer.model_validate(args)
            except Exception as exc:  # noqa: BLE001
                return f"Error: {exc}"
            return await on_ask(payload.question, payload.wait)
        if name == "propose_contract_change":
            if on_propose is None:
                return "Error: contract changes are not available here — record a known_gap"
            try:
                payload = ProposeContractChange.model_validate(args)
            except Exception as exc:  # noqa: BLE001
                return f"Error: {exc}"
            return await on_propose(payload)
        if name == "escalate":
            if on_escalate is None:
                return (
                    "Error: no tech lead on this run — decide from the spec and plan, and "
                    "record what you assumed in known_gaps"
                )
            try:
                payload = Escalate.model_validate(args)
            except Exception as exc:  # noqa: BLE001
                return f"Error: {exc}"
            return await on_escalate(payload)
        if name == "request_files":
            if on_request_files is None:
                return (
                    "Error: shared-file requests are not available here — record what "
                    "you need in known_gaps"
                )
            try:
                payload = RequestFiles.model_validate(args)
            except Exception as exc:  # noqa: BLE001
                return f"Error: {exc}"
            return await on_request_files(list(payload.paths), payload.reason, payload.wait)
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
