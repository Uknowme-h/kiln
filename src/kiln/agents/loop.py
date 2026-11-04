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
  the model as ordinary tool output so it adapts instead of dying;
- prune old tool bodies and optional compaction so long loops stay cheap.
"""

import asyncio
import inspect
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from kiln.agents.context import DEFAULT_PRUNE_PROTECT_CHARS, prune_tool_results
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
COMPACT_KEEP_TAIL = 6
# What the summarizer may read of the conversation. The brief is excluded (it
# is re-injected verbatim), so this is all history; when it must be clipped the
# most recent part wins, because that is what the agent is in the middle of.
COMPACT_INPUT_CHARS = 60_000
# The brief survives compaction whole up to this size. It carries the contract,
# design tokens and the tests to read; clipping it to 6k (the old cap) left the
# agent coding against a reminder digest after its first compaction.
COMPACT_BRIEF_CHARS = 30_000
# Batch pruning: rewrite stale tool bodies only once this much is reclaimable.
# Rewriting history every step invalidates a provider's cached prompt prefix
# from the first rewritten message onward, so the whole tail is re-billed.
PRUNE_MIN_RECLAIM_CHARS = 20_000
# Turns the model gets to produce a valid finish payload once the cap hits.
# One was not enough: run-afe452e5's worker had written every file of its task
# and then sent submit_result with empty arguments; the single rejection ended
# the run and threw the finished work away.
HANDOFF_ATTEMPTS = 3
WRITE_TOOLS = frozenset({"write_file", "edit_file"})

HANDOFF = (
    "You are out of steps. Tools are withdrawn except submit_result. Call submit_result "
    "now with every required field filled in: report what you accomplished, what "
    "remains unfinished (as known_gaps), and the next step you would recommend. Do not "
    "claim work you did not do."
)

COMPACT_SYSTEM = (
    "Summarize this agent conversation for continued work. Use these headings:\n"
    "## Objective\n## Decisions\n## Files written or touched\n"
    "## Verified (already checked — do not re-check)\n## Blockers\n## Next\n"
    "Keep concrete paths and open todos. Omit raw file dumps and long logs."
)


class AgentLoopError(RuntimeError):
    """The agent could not finish: it stalled, got stuck, or ran past its cap.

    ``written_paths`` lists the files the agent wrote successfully before it
    failed, so a caller can salvage real on-disk work instead of discarding it.
    """

    def __init__(self, message: str, *, written_paths: list[str] | tuple[str, ...] = ()):
        super().__init__(message)
        self.written_paths = list(written_paths)


def _describe_rejection(exc: ValidationError, finish: type[BaseModel]) -> str:
    """A rejection the model can act on: what was wrong and what is required."""
    problems = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ())) or "(payload)"
        problems.append(f"{loc}: {err.get('msg', 'invalid')}")
    required = [name for name, field in finish.model_fields.items() if field.is_required()]
    return "; ".join(problems) + f". Required fields: {', '.join(required)}."


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


def _history_for_summary(history: list[dict], limit: int = COMPACT_INPUT_CHARS) -> str:
    """The conversation after the brief, newest kept when it must be clipped.

    Each message is rendered compactly; tool bodies are clipped so one huge
    output cannot crowd out the decisions around it.
    """
    rendered: list[str] = []
    for message in history:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if role == "tool" and len(content) > 2_000:
            content = content[:1_500] + f"\n…[{len(content) - 1_500} chars]"
        calls = message.get("tool_calls") or []
        if calls:
            names = ", ".join(
                f"{c['function']['name']}({c['function'].get('arguments', '')[:200]})"
                for c in calls
            )
            content = f"{content}\n[tool calls: {names}]".strip()
        rendered.append(f"[{role}] {content}")
    text = "\n\n".join(rendered)
    if len(text) <= limit:
        return text
    return f"…[{len(text) - limit} earlier chars omitted]…\n" + text[-limit:]


def _safe_tail(history: list[dict], keep: int) -> list[dict]:
    """The last ``keep`` messages, never opening on an orphaned tool result.

    A slice can cut between an assistant turn and the tool results answering
    its tool_calls; the API rejects a ``tool`` message with no preceding call
    (HTTP 400 — run-9bbe61f0's worker-1 died that way right after its first
    compaction). Drop leading tool results until the tail opens on a turn.
    """
    tail = history[-keep:] if keep > 0 else []
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    return tail


def _brief_for_reinjection(brief: str, limit: int = COMPACT_BRIEF_CHARS) -> str:
    if len(brief) <= limit:
        return brief
    head = limit * 3 // 4
    return (
        brief[:head]
        + "\n…[brief clipped after compaction; re-read docs with read_file]…\n"
        + brief[-(limit - head):]
    )


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
    check: Callable[[T], str | None | Awaitable[str | None]] | None = None,
    on_call: OnCall | None = None,
    transcript: TranscriptLog | None = None,
    agent_id: str | None = None,
    reminder: str | None = None,
    route=None,
    todo_store=None,
    todo_probe_every: int = 5,
    enable_compaction: bool = True,
    compact_threshold_chars: int = 100_000,
    prune_protect_chars: int = DEFAULT_PRUNE_PROTECT_CHARS,
) -> LoopOutcome[T]:
    """Drive one agent to a validated `finish` payload, or raise `AgentLoopError`.

    `reminder` re-asserts the mission-critical context (for a dev, the
    contract) when the model is most likely to have drifted: doom-loop
    refusal, step-cap handoff, and after compaction.
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

    def _prepare_for_model() -> None:
        n = prune_tool_results(
            messages,
            protect_chars=prune_protect_chars,
            min_reclaim_chars=PRUNE_MIN_RECLAIM_CHARS,
        )
        if n:
            _log("note", {"text": "tool results pruned", "pruned": n})

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
    # what the agent actually put on disk (ordered, unique), and when it last
    # made progress — evidence for salvage, the handoff, compaction and the probe
    written: dict[str, None] = {}
    current_step = 0
    last_progress_step = 0

    def _written_block(lead: str) -> str:
        if not written:
            return ""
        return f"\n\n{lead}\n" + "\n".join(f"- {path}" for path in written)

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
                    {"role": "system", "content": COMPACT_SYSTEM},
                    {
                        "role": "user",
                        # history only: the brief is re-injected below, so feeding
                        # it here just crowded the real work out of the window
                        "content": _history_for_summary(messages[2:]) or "(no history yet)",
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
            original = ""
            if len(messages) > 1 and isinstance(messages[1].get("content"), str):
                original_body = _brief_for_reinjection(messages[1]["content"])
                original = f"<original_task>\n{original_body}\n</original_task>\n\n"
            # Drop system + original user (re-injected as <original_task>); keep
            # only a recent conversation tail so history is not paid twice.
            keep_tail = _safe_tail(messages[2:], COMPACT_KEEP_TAIL)
            messages[:] = [
                messages[0],
                {
                    "role": "user",
                    "content": (
                        f"{original}"
                        f"Conversation summary after compaction:\n{summary}{todos}"
                        # deterministic, not the summarizer's memory: without this the
                        # agent re-read and re-verified everything it had just written
                        + _written_block(
                            "Files you already wrote this task (on disk — do not re-read "
                            "or re-verify them unless you must change one):"
                        )
                        + "\n\n"
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
        nonlocal strikes, calls_made, last_tool_chars, last_progress_step
        args, problem = _parse_arguments(call.arguments)
        if args is None:
            return problem, None
        if call.name == finish_name:
            # Validated before the doom guard: an invalid submit is rejected
            # every time anyway, and the model needs the actionable reason, not
            # "you repeated this call". Rejections are logged so a transcript
            # shows why a handoff failed (run-afe452e5's did not).
            _log("tool_call", {"name": call.name, "args": args})
            try:
                payload = finish.model_validate(args)
            except ValidationError as exc:
                reason = _describe_rejection(exc, finish)
                _log("tool_result", {"name": call.name, "error": reason})
                return f"submit_result was rejected: {reason}\nFix it and call submit_result again.", None
            complaint = check(payload) if check else None
            if inspect.isawaitable(complaint):
                complaint = await complaint  # checks may look at the workspace
            if complaint:
                _log("tool_result", {"name": call.name, "error": complaint})
                return f"submit_result was rejected: {complaint}", None
            _log("tool_result", {"name": call.name, "output": "accepted"})
            return "accepted", payload
        key = (call.name, json.dumps(args, sort_keys=True))
        seen[key] += 1
        if seen[key] >= DOOM_REPEATS:
            strikes += 1
            if strikes >= DOOM_STRIKES:
                raise AgentLoopError(
                    "the agent is stuck repeating the same call", written_paths=list(written)
                )
            return (
                _with_reminder(
                    f"Refused: you have made this exact {call.name} call {seen[key]} times. "
                    "Change your approach, or call submit_result with what you have."
                ),
                None,
            )
        if call.name not in names:
            return f"Unknown tool {call.name!r}. Available: {', '.join(sorted(names))}.", None
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
        if call.name in WRITE_TOOLS and isinstance(args.get("path"), str):
            written[args["path"]] = None
            last_progress_step = current_step
        return clipped, None

    for step in range(1, max_steps + 1):
        current_step = step
        if todo_store is not None and todo_probe_every > 0 and step > 1:
            # Probe only a stalled agent. Probing one that is writing files every
            # step just bought a todo-only turn (~20k prompt tokens each, seven of
            # them in run-afe452e5's worker-1).
            stalled = step - last_progress_step >= todo_probe_every
            if step % todo_probe_every == 0 and stalled and todo_store.open_items():
                messages.append(
                    {
                        "role": "user",
                        "content": _with_reminder(
                            "Goal probe: no file progress for a while and todos remain "
                            "open. Do the next todo now; mark items done with todo_write "
                            "in the same turn as the work, not as a turn of its own."
                        ),
                    }
                )
        await maybe_compact()
        _prepare_for_model()
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
                raise AgentLoopError(
                    "the agent stopped without submitting a result", written_paths=list(written)
                )
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

    # The cap hit: withdraw every tool but the finish tool and demand a handoff.
    # The model gets HANDOFF_ATTEMPTS turns, each told exactly what was wrong
    # with the last one, and is reminded which files it actually wrote so it
    # can fill files_written truthfully.
    messages.append(
        {
            "role": "user",
            "content": _with_reminder(
                HANDOFF + _written_block("Files you wrote this task (from the tool log):")
            ),
        }
    )
    only_finish = [tool for tool in tools if tool["function"]["name"] == finish_name]
    for attempt in range(1, HANDOFF_ATTEMPTS + 1):
        _prepare_for_model()
        completion = await asyncio.to_thread(
            gateway.complete,
            role,
            messages,
            max_tokens=max_tokens,
            tools=only_finish,
            agent_id=aid,
            route=route,
        )
        finish_calls = [call for call in completion.tool_calls if call.name == finish_name]
        if not finish_calls:
            messages.append({"role": "assistant", "content": completion.content.strip() or "(no text)"})
            messages.append(
                {
                    "role": "user",
                    "content": f"Call {finish_name} now (handoff attempt {attempt} of "
                    f"{HANDOFF_ATTEMPTS}); no other tool is available.",
                }
            )
            continue
        messages.append(_assistant_message(completion.content or "", finish_calls))
        for call in finish_calls:
            text, payload = await handle(call)
            if payload is not None:
                return LoopOutcome(payload, steps=max_steps, tool_calls=calls_made)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text or "(no output)"})
    raise AgentLoopError(
        f"step cap ({max_steps}) reached without a valid submit_result",
        written_paths=list(written),
    )
