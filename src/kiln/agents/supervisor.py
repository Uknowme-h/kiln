"""Tech lead: one structured decision per escalation.

Called by the orchestrator only when a developer escalates or the build hits
trouble — never on every step — so supervision costs a handful of calls per
run. The lead reads a compact context and returns a ``Decision``; it has no
tools. A reply that fails validation is retried once with the error shown.
"""

from __future__ import annotations

from pydantic import ValidationError

from kiln.agents.briefing import clip_text
from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.supervisor import Decision, Escalation

MAX_DISCOVERY = 3000
MAX_PLAN = 3000
MAX_BOARD = 6000
MAX_ENV = 2000


class SupervisorError(RuntimeError):
    """The lead could not produce a valid decision."""


def build_context(
    *,
    idea: str,
    spec,
    discovery: str,
    plan_memo: str,
    board: str,
    escalation: Escalation,
    environment: str = "",
    decisions: list[str] | None = None,
) -> str:
    criteria = "\n".join(
        f"- {c.id}: given {c.given}, when {c.when}, then {c.then}"
        for c in (spec.acceptance_criteria if spec else [])
    )
    parts = [
        f"<user_idea>\n{idea}\n</user_idea>",
        f"<spec>\n{spec.title if spec else ''}: {spec.summary if spec else ''}\n"
        f"Stack: {spec.stack if spec else ''}\nAcceptance criteria:\n{criteria}\n</spec>",
    ]
    if discovery.strip():
        parts.append(f"<discovery>\n{clip_text(discovery.strip(), MAX_DISCOVERY)}\n</discovery>")
    if plan_memo.strip():
        parts.append(f"<plan_memo>\n{clip_text(plan_memo.strip(), MAX_PLAN)}\n</plan_memo>")
    if board.strip():
        parts.append(clip_text(board.strip(), MAX_BOARD))
    if decisions:
        parts.append("<your_earlier_decisions>\n" + "\n".join(f"- {d}" for d in decisions[-8:])
                     + "\n</your_earlier_decisions>")
    if environment.strip():
        parts.append(f"<environment>\n{clip_text(environment.strip(), MAX_ENV)}\n</environment>")
    parts.append(
        "<escalation>\n"
        f"kind: {escalation.kind}\nfrom: {escalation.source or 'unknown'}\n"
        f"task: {escalation.task_id or 'none'}\nsummary: {escalation.summary}\n"
        f"details:\n{escalation.details or '(none)'}\n</escalation>"
    )
    return "\n\n".join(parts)


def _messages(context: str, error: str | None) -> list[dict[str, str]]:
    user = "Decide how the team proceeds. Answer with the decision JSON only.\n\n" + context
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Return corrected JSON only."
        )
    return [
        {"role": "system", "content": system_prompt_for(Role.SUPERVISOR)},
        {"role": "user", "content": user},
    ]


def _attempt(gateway: Gateway, messages: list[dict[str, str]]) -> Decision:
    completion = gateway.complete(
        Role.SUPERVISOR,
        messages,
        response_format=response_format("lead_decision", Decision),
        max_tokens=definition_for(Role.SUPERVISOR).max_output_tokens,
        agent_id=Role.SUPERVISOR.value,
    )
    return Decision.model_validate_json(completion.content)


def decide(gateway: Gateway, context: str) -> Decision:
    """One decision. One retry with the validation error shown."""
    try:
        return _attempt(gateway, _messages(context, None))
    except ValidationError as first:
        try:
            return _attempt(gateway, _messages(context, str(first)))
        except ValidationError as second:
            raise SupervisorError(f"the lead produced an invalid decision twice: {second}") from second
