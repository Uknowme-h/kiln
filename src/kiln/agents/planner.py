"""PM planner: spec to a task graph with dependencies and file ownership.

Two shapes, like the requirements analyst:
- `run_agentic`: a short read-only exploration loop on the user's machine
  (read/list/glob/grep, one optional `ask_user`, and exactly one write —
  `docs/plan.md`, enforced by the companion's permissions and leases, not by
  the prompt) followed by the structured task-graph call;
- `run`: the one-shot structured call, kept as the offline fallback.

The model's reply must validate as a `TaskGraph` on every run — unique ids,
valid dependencies, no cycles, no overlapping owned paths. A reply that fails
validation is retried once with the error shown, then counted as a failure.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import Contract, TaskGraph
from kiln.tools.registry import specs_for

CallTool = Callable[[str, dict], Awaitable[str]]

# Exploration is a bonus, not the job: a few turns of looking before planning.
MAX_EXPLORE_ROUNDS = 4
PLAN_DOC = "docs/plan.md"
CONTRACT_DOC = "docs/contract.md"
# owned by the scaffold task on UI briefs; the reviewer and reporter read it
DESIGN_DOC = "docs/design.md"


class PlannerError(RuntimeError):
    """The planner could not produce a valid task graph."""


def _base_messages(
    spec: Spec, user_notes: tuple[str, ...], *, ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False
) -> list[dict[str, str]]:
    spec_block = f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>"
    if user_notes:
        notes = "\n".join(f"- {note}" for note in user_notes)
        spec_block += f"\n\n<user_notes>\n{notes}\n</user_notes>"
    return [
        {
            "role": "system",
            "content": system_prompt_for(Role.PLANNER, ui_product=ui_product, prod_ops=prod_ops, security=security),
        },
        {"role": "user", "content": spec_block},
    ]


def _graph_messages(
    spec: Spec,
    messages: list[dict[str, str]],
    user_notes: tuple[str, ...],
    error: str | None,
    *,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> list[dict[str, str]]:
    user = "Plan this spec into a task graph. Answer with the task graph JSON only."
    retry = ""
    if error is not None:
        retry = (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: overlapping owned paths, "
            "a dependency cycle, or a task with no owned paths. Return corrected JSON only."
        )
    if not messages:
        # one-shot: a single user message carries the ask, the spec, and any retry error
        spec_block = f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>"
        if user_notes:
            notes = "\n".join(f"- {note}" for note in user_notes)
            spec_block += f"\n\n<user_notes>\n{notes}\n</user_notes>"
        return [
            {
                "role": "system",
                "content": system_prompt_for(Role.PLANNER, ui_product=ui_product, prod_ops=prod_ops, security=security),
            },
            {"role": "user", "content": f"{user}\n\n{spec_block}{retry}"},
        ]
    working = list(messages)
    working.append({"role": "user", "content": user + retry})
    return working


def _attempt(gateway: Gateway, messages: list[dict[str, str]]) -> TaskGraph:
    completion = gateway.complete(
        Role.PLANNER,
        messages,
        response_format=response_format("task_graph", TaskGraph),
        max_tokens=definition_for(Role.PLANNER).max_output_tokens,
    )
    if completion.truncated:
        raise PlannerError(
            "the planner's output was truncated before the task graph finished. "
            "Raise max_output_tokens in config (.env) — it is a runaway guard, not "
            "a length limit (the length guidance lives in the system prompt)."
        )
    return TaskGraph.model_validate_json(completion.content)


def run(
    gateway: Gateway,
    spec: Spec,
    user_notes: tuple[str, ...] = (),
    *,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> TaskGraph:
    """Turn a spec into a validated task graph. One retry with the error shown."""
    try:
        return _attempt(
            gateway,
            _graph_messages(
                spec,
                [],
                user_notes,
                None,
                ui_product=ui_product,
                prod_ops=prod_ops,
                security=security,
            ),
        )
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                _graph_messages(
                    spec,
                    [],
                    user_notes,
                    str(first),
                    ui_product=ui_product,
                    prod_ops=prod_ops,
                    security=security,
                ),
            )
        except ValidationError as second:
            raise PlannerError(
                f"planner produced an invalid task graph twice: {second}"
            ) from second


async def _explore(
    gateway: Gateway,
    messages: list[dict[str, str]],
    call_tool: CallTool,
) -> list[dict[str, str]]:
    """Read-only turns before planning; ends when the model stops calling tools."""
    tools = specs_for(definition_for(Role.PLANNER).tools)
    working = list(messages)
    for _ in range(MAX_EXPLORE_ROUNDS):
        completion = await asyncio.to_thread(
            gateway.complete,
            Role.PLANNER,
            working,
            tools=tools,
            agent_id=Role.PLANNER.value,
        )
        if not completion.tool_calls:
            if completion.content:
                working.append({"role": "assistant", "content": completion.content})
            break
        working.append(
            {
                "role": "assistant",
                "content": completion.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments or "{}"},
                    }
                    for call in completion.tool_calls
                ],
            }
        )
        for call in completion.tool_calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                output = await call_tool(call.name, args if isinstance(args, dict) else {})
            except Exception as exc:
                output = f"Error: {exc}"
            working.append(
                {"role": "tool", "tool_call_id": call.id, "content": output or "(no output)"}
            )
    return working


async def run_agentic(
    gateway: Gateway,
    spec: Spec,
    user_notes: tuple[str, ...] = (),
    *,
    call_tool: CallTool,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> TaskGraph:
    """Explore (read-only, one plan note) then emit the validated task graph.

    Falls back to the one-shot path when the exploration itself fails, so a
    shaky tool turn never costs the run its plan.
    """
    skill = dict(ui_product=ui_product, prod_ops=prod_ops, security=security)
    messages = _base_messages(spec, user_notes, **skill)
    try:
        messages = await _explore(gateway, messages, call_tool)
    except Exception:
        messages = _base_messages(spec, user_notes, **skill)
    try:
        return _attempt(
            gateway,
            _graph_messages(spec, messages, user_notes, None, **skill),
        )
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                _graph_messages(
                    spec, messages, user_notes, str(first), **skill
                ),
            )
        except ValidationError as second:
            raise PlannerError(
                f"planner produced an invalid task graph twice: {second}"
            ) from second


# ── the durable plan documents ───────────────────────────────────────────────


def render_plan_markdown(spec: Spec, graph: TaskGraph) -> str:
    """`docs/contract.md`: the machine contract plus the task table, rendered
    deterministically from the validated graph so the whole team (and the user)
    can re-read exactly what the workers coded against."""
    lines = [
        f"# Contract — {graph.spec_title}",
        "",
        f"Spec: {spec.title} (stack: {spec.stack_id})",
        "",
    ]
    contract = graph.contract or Contract()
    if contract.modules:
        lines.append("## Modules")
        lines += [f"- `{module}`" for module in contract.modules]
        lines.append("")
    if contract.routes:
        lines.append("## Routes")
        for route in contract.routes:
            entry = f"- `{route.method} {route.path}`"
            if route.request or route.response:
                entry += f" — req: {route.request or '–'} → res: {route.response or '–'}"
            lines.append(entry)
        lines.append("")
    if contract.shared_files:
        lines.append("## Shared files (scaffold owns)")
        lines += [f"- `{path}`" for path in contract.shared_files]
        lines.append("")
    lines.append("## Tasks")
    for task in graph.tasks:
        deps = f" (after {', '.join(task.depends_on)})" if task.depends_on else ""
        covers = f" — covers {', '.join(task.covers)}" if task.covers else ""
        lines.append(
            f"- **{task.id}** [{task.kind}]{deps}: {task.title}{covers}\n"
            f"  owns: {', '.join(task.owned_paths)}"
        )
    lines.append("")
    return "\n".join(lines)
