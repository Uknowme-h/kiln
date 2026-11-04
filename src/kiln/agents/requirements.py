"""Requirements analyst: idea text to a typed spec.

Understands the idea (research + optional ask_user — model invents questions),
writes a free-form docs/discovery.md decisions log, then produces a Spec that
validates on every run. A reply that fails validation is retried once with the
error shown.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from kiln.agents.briefing import existing_workspace_block
from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.roles import Role
from kiln.agents.skills import (
    needs_prod_ops_skill,
    needs_security_skill,
    needs_ui_product_skill,
)
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.tools.registry import specs_for

CallTool = Callable[[str, dict], Awaitable[str]]

# Free-form decisions log written before the Spec (agent-driven clarify).
DISCOVERY_DOC = "docs/discovery.md"

# Clarify/research turns: ask_user topics are model-chosen (prompt-level);
# the tool-loop cap bounds the phase — a look at an existing workspace, research
# (search → fetch), up to two question rounds, and the discovery write.
MAX_TOOL_ROUNDS = 6

OPENING = (
    "Understand this brief before you specify it. Ask the user only about what "
    "this specific brief leaves open and research or the workspace cannot "
    "settle — there is no standard question list, and a brief that already "
    "answers everything needs no questions. Specify the product the user "
    "described at the ambition they described; do not assume a first version "
    "or a reduced scope unless they asked for one. When you have enough, stop "
    "calling tools — the next turn will ask you for the spec JSON."
)


class RequirementsError(RuntimeError):
    """The analyst could not produce a valid spec."""


def _base_messages(
    idea: str, user_notes: tuple[str, ...], inventory: list[str] | None = None
) -> list[dict]:
    idea_block = f"<user_idea>\n{idea}\n</user_idea>"
    if user_notes:
        notes = "\n".join(f"- {note}" for note in user_notes)
        idea_block += f"\n\n<user_notes>\n{notes}\n</user_notes>"
    idea_block += existing_workspace_block(inventory)
    return [
        {
            "role": "system",
            "content": system_prompt_for(
                Role.REQUIREMENTS,
                ui_product=needs_ui_product_skill(idea=idea),
                prod_ops=needs_prod_ops_skill(idea=idea),
                security=needs_security_skill(idea=idea),
            ),
        },
        {
            "role": "user",
            "content": OPENING + "\n\n" + idea_block,
        },
    ]


def _spec_messages(messages: list[dict], error: str | None) -> list[dict]:
    user = (
        "Write the specification for this product idea now. Answer with the "
        "spec JSON only. Set stack_id to exactly \"python\" or \"node\", name "
        "the frameworks (fastapi/flask/django/express/nestjs/vite_react/nextjs), "
        "and set roots for a multi-root product (an API + a Vite SPA split into "
        "backend/ + frontend/; nextjs alone may be one root)."
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Do not explain; return corrected JSON only."
        )
    return [*messages, {"role": "user", "content": user}]


def _attempt(gateway: Gateway, messages: list[dict]) -> Spec:
    completion = gateway.complete(
        Role.REQUIREMENTS,
        messages,
        response_format=response_format("spec", Spec),
        max_tokens=definition_for(Role.REQUIREMENTS).max_output_tokens,
    )
    return Spec.model_validate_json(completion.content)


async def _clarify(gateway: Gateway, messages: list[dict], call_tool: CallTool) -> list[dict]:
    """Up to MAX_TOOL_ROUNDS of tool turns: look around, questions, research, discovery file."""
    tools = specs_for(definition_for(Role.REQUIREMENTS).tools)
    working = list(messages)
    for _ in range(MAX_TOOL_ROUNDS):
        completion = await asyncio.to_thread(
            gateway.complete,
            Role.REQUIREMENTS,
            working,
            tools=tools,
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
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments or "{}",
                        },
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


async def run_async(
    gateway: Gateway,
    idea: str,
    user_notes: tuple[str, ...] = (),
    *,
    call_tool: CallTool | None = None,
    ask: bool = True,
    inventory: list[str] | None = None,
) -> Spec:
    if not idea.strip():
        raise RequirementsError("a run needs an idea")
    messages = _base_messages(idea, user_notes, inventory)
    if ask and call_tool is not None:
        messages = await _clarify(gateway, messages, call_tool)
    try:
        return await asyncio.to_thread(_attempt, gateway, _spec_messages(messages, None))
    except ValidationError as first:
        try:
            return await asyncio.to_thread(
                _attempt, gateway, _spec_messages(messages, str(first))
            )
        except ValidationError as second:
            raise RequirementsError(
                f"requirements analyst produced an invalid spec twice: {second}"
            ) from second


def run(gateway: Gateway, idea: str, user_notes: tuple[str, ...] = ()) -> Spec:
    """Sync path used by tests and the non-asking fallback."""
    return asyncio.run(run_async(gateway, idea, user_notes, ask=False))
