"""The tool-using agent loop.

One agent, one conversation: the model calls tools until it calls the finish
tool with a payload that validates. Everything the model can touch goes
through `call_tool`, so the relay, the companion's permission gate, and the
path leases sit behind the same function for every agent.

Guards, in the spirit of OpenCode's runner:
- a step cap that does not just stop: tools are withdrawn except the finish
  tool, and the agent must hand over what it accomplished and what is left;
- a doom-loop guard that refuses the third identical call and gives up after
  repeated strikes;
- tool failures (including a user's refusal, with their feedback) go back to
  the model as ordinary tool output so it adapts instead of dying.
"""

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from kiln.agents.roles import Role
from kiln.agents.transcript import TranscriptLog
from kiln.gateway.client import Gateway

T = TypeVar("T", bound=BaseModel)

CallTool = Callable[[str, dict], Awaitable[str]]
OnCall = Callable[[str, dict], Awaitable[None]]

MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_NUDGES = 2
DOOM_REPEATS = 3
DOOM_STRIKES = 3

HANDOFF = (
    "You are out of steps. Tools are withdrawn except submit_result. Call submit_result "
    "now: report what you accomplished, what remains unfinished (as known_gaps), and "
    "the next step you would recommend. Do not claim work you did not do."
)


class AgentLoopError(RuntimeError):
    """The agent could not finish: it stalled, got stuck, or ran past its cap."""


@dataclass
class LoopOutcome(Generic[T]):
    result: T
    steps: int
    tool_calls: int


def _clip(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    head = MAX_TOOL_OUTPUT_CHARS * 2 // 3
    tail = MAX_TOOL_OUTPUT_CHARS - head
    return f"{text[:head]}\n...[{len(text) - head - tail} characters trimmed]...\n{text[-tail:]}"


def _assistant_message(content: str, calls) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments or "{}"},
            }
            for call in calls
        ],
    }


def _parse_arguments(raw: str) -> tuple[dict | None, str]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return None, f"The tool arguments were not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "The tool arguments must be a JSON object."
    return {key: value for key, value in parsed.items() if value is not None}, ""


def _message_chars(messages: list[dict]) -> int:
    return sum(len(json.dumps(message, default=str)) for message in messages)


async def run_agent_loop(
    *,
    gateway: Gateway,
    role: Role,
    system: str,
    user: str,
    tools: list[dict],
    call_tool: CallTool,
    finish: type[T],
    max_steps: int,
    finish_name: str = "submit_result",
    max_tokens: int | None = None,
    check: Callable[[T], str | None] | None = None,
    on_call: OnCall | None = None,
    transcript: TranscriptLog | None = None,
    agent_id: str | None = None,
    reminder: str | None = None,
    route=None,
    todo_store=None,
    todo_probe_every: int = 5,
    enable_compaction: bool = False,
    compact_threshold_chars: int = 100_000,
) -> LoopOutcome[T]:
    """Drive one agent to a validated `finish` payload, or raise `AgentLoopError`.

    `reminder` re-asserts the mission-critical context (for a dev, the
    contract) exactly when the model is most likely to have drifted: on a
    doom-loop refusal and at the step-cap handoff. Same idea as OpenCode's
    per-request reminder reconciliation, adapted to a loop with no compaction.
    """
    names = {tool["function"]["name"] for tool in tools}
    aid = agent_id or role.value

    def _log(kind: str, payload: dict) -> None:
        if transcript is not None:
            transcript.record(aid, role.value, kind, payload)

    def _with_reminder(text: str) -> str:
        extra = ""
        if todo_store is not None and todo_store.open_items():
            extra = f"\n\nOpen todos:\n{todo_store.render()}"
        if reminder:
            return f"{text}\n\nReminder — stay on the contract:\n{reminder}{extra}"
        return f"{text}{extra}" if extra else text

    if finish_name not in names:
        raise ValueError(f"the tool list must include {finish_name}")
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    seen: Counter[tuple[str, str]] = Counter()
    strikes = 0
    nudges = 0
    calls_made = 0
    compact_count = 0
    thrash_skips = 0
    last_tool_chars = 0

    async def maybe_compact() -> None:
        nonlocal compact_count, thrash_skips, messages
        if not enable_compaction:
            return
        if compact_count >= 2:
            return
        size = _message_chars(messages)
        if size < compact_threshold_chars:
            return
        if last_tool_chars > compact_threshold_chars // 2 and thrash_skips < 2:
            thrash_skips += 1
            messages.append(
                {
                    "role": "user",
                    "content": _with_reminder(
                        "Context is large after a huge tool result — do not re-dump it. "
                        "Use spawn_explore or ranged read_file; prefer path references."
                    ),
                }
            )
            return
        try:
            summary_completion = await asyncio.to_thread(
                gateway.complete,
                role,
                [
                    {
                        "role": "system",
                        "content": (
                            "Summarize this agent conversation for continued work. "
                            "Keep: task goal, files written, open todos, key decisions, "
                            "and blockers. Omit raw file dumps."
                        ),
                    },
                    {
                        "role": "user",
                        "content": _clip(json.dumps(messages[1:], default=str)),
                    },
                ],
                max_tokens=max_tokens,
                agent_id=f"{aid}/compact",
                route=route,
            )
            summary = (summary_completion.content or "").strip() or "(empty summary)"
            todos = ""
            if todo_store is not None:
                todos = f"\n\nTodos:\n{todo_store.render()}"
            # Keep a shortened original task (often carries <design>/<discovery>/contract).
            original = ""
            if len(messages) > 1 and isinstance(messages[1].get("content"), str):
                original_body = messages[1]["content"]
                if len(original_body) > 6000:
                    original_body = (
                        original_body[:4500]
                        + "\n…[task clipped after compact; use read_file for docs]…\n"
                        + original_body[-1200:]
                    )
                original = f"<original_task>\n{original_body}\n</original_task>\n\n"
            keep_tail = messages[-4:] if len(messages) > 4 else messages[2:]
            messages[:] = [
                messages[0],
                {
                    "role": "user",
                    "content": (
                        f"{original}"
                        f"Conversation summary after compaction:\n{summary}{todos}\n\n"
                        + (
                            f"Reminder — stay on the contract:\n{reminder}\n\n"
                            if reminder
                            else ""
                        )
                        + "Continue from here."
                    ),
                },
                *keep_tail,
            ]
            compact_count += 1
            _log("note", {"text": "context compacted", "compact_count": compact_count})
        except Exception:
            return

    async def handle(call) -> tuple[str, T | None]:
        """Run one tool call; the second item is set only by a valid finish."""
        nonlocal strikes, calls_made, last_tool_chars
        args, problem = _parse_arguments(call.arguments)
        if args is None:
            return problem, None
        key = (call.name, json.dumps(args, sort_keys=True))
        seen[key] += 1
        if seen[key] >= DOOM_REPEATS:
            strikes += 1
            if strikes >= DOOM_STRIKES:
                raise AgentLoopError("the agent is stuck repeating the same call")
            return (
                _with_reminder(
                    f"Refused: you have made this exact {call.name} call {seen[key]} times. "
                    "Change your approach, or call submit_result with what you have."
                ),
                None,
            )
        if call.name not in names:
            return f"Unknown tool {call.name!r}. Available: {', '.join(sorted(names))}.", None
        if call.name == finish_name:
            try:
                payload = finish.model_validate(args)
            except ValidationError as exc:
                return f"submit_result was rejected: {exc}\nFix it and call submit_result again.", None
            complaint = check(payload) if check else None
            if complaint:
                return f"submit_result was rejected: {complaint}", None
            _log("tool_call", {"name": call.name, "args": args})
            _log("tool_result", {"name": call.name, "output": "accepted"})
            return "accepted", payload
        calls_made += 1
        if on_call is not None:
            await on_call(call.name, args)
        _log("tool_call", {"name": call.name, "args": args})
        try:
            output = await call_tool(call.name, args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a refusal or failure is data for the model, not a crash
            _log("tool_result", {"name": call.name, "error": str(exc)})
            return f"Error: {exc}", None
        clipped = _clip(output or "(no output)")
        last_tool_chars = len(clipped)
        _log("tool_result", {"name": call.name, "output": clipped})
        return clipped, None

    for step in range(1, max_steps + 1):
        if todo_store is not None and todo_probe_every > 0 and step > 1:
            if step % todo_probe_every == 0 and todo_store.open_items():
                messages.append(
                    {
                        "role": "user",
                        "content": _with_reminder(
                            "Goal probe: unfinished todos remain. Address them or "
                            "update todo_write before more speculative work."
                        ),
                    }
                )
        await maybe_compact()
        completion = await asyncio.to_thread(
            gateway.complete,
            role,
            messages,
            max_tokens=max_tokens,
            tools=tools,
            agent_id=aid,
            route=route,
        )
        if not completion.tool_calls:
            nudges += 1
            if nudges > MAX_NUDGES:
                raise AgentLoopError("the agent stopped without submitting a result")
            messages.append({"role": "assistant", "content": completion.content.strip() or "(no text)"})
            messages.append(
                {
                    "role": "user",
                    "content": "Keep going with the tools, or call submit_result if you are done.",
                }
            )
            continue
        messages.append(_assistant_message(completion.content or "", completion.tool_calls))
        for call in completion.tool_calls:
            text, payload = await handle(call)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text or "(no output)"})
            if payload is not None:
                return LoopOutcome(payload, steps=step, tool_calls=calls_made)

    # the cap hit: withdraw every tool but the finish tool and demand a handoff
    messages.append({"role": "user", "content": _with_reminder(HANDOFF)})
    only_finish = [tool for tool in tools if tool["function"]["name"] == finish_name]
    completion = await asyncio.to_thread(
        gateway.complete,
        role,
        messages,
        max_tokens=max_tokens,
        tools=only_finish,
        agent_id=aid,
        route=route,
    )
    for call in completion.tool_calls:
        if call.name != finish_name:
            continue
        text, payload = await handle(call)
        if payload is not None:
            return LoopOutcome(payload, steps=max_steps, tool_calls=calls_made)
    raise AgentLoopError(f"step cap ({max_steps}) reached without a valid submit_result")
