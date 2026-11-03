"""Brief-reviewer: does the green app deliver the idea? Evidence only.

Two modes like the reporter:
- one-shot structured JSON verdict (legacy / tests): the orchestrator passes a
  workspace snapshot, since a one-shot call has no tools;
- agentic pass: read-only tools verify claims against the real files.

The reply must validate as a `ReviewVerdict` on every run; a reply that fails
validation is retried once with the error shown.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.loop import AgentLoopError, run_agent_loop
from kiln.agents.roles import Role
from kiln.agents.transcript import TranscriptLog
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.results import TestReport
from kiln.schemas.review import ReviewVerdict
from kiln.schemas.spec import Spec
from kiln.tools.registry import ToolSpec, specs_for

CallTool = Callable[[str, dict], Awaitable[str]]

MAX_FILE_CHARS = 4000
MAX_FILES = 25


class ReviewerError(RuntimeError):
    """The reviewer could not produce a valid verdict."""


def _test_report_block(report: TestReport) -> str:
    return (
        f"passed={report.passed} failed={report.failed} "
        f"errors={report.errors} skipped={report.skipped} green={report.green}"
    )


def _snapshot_block(snapshot: dict[str, str]) -> str:
    if not snapshot:
        return "(no files read)"
    parts = []
    for path, content in list(snapshot.items())[:MAX_FILES]:
        trimmed = content[:MAX_FILE_CHARS]
        if len(content) > MAX_FILE_CHARS:
            trimmed += "\n# ... trimmed ..."
        parts.append(f'<file path="{path}">\n{trimmed}\n</file>')
    return "\n\n".join(parts)


def _body(
    idea: str,
    spec: Spec,
    discovery: str,
    design: str,
    report: TestReport,
    snapshot: dict[str, str],
    opening: str,
) -> str:
    discovery_block = discovery.strip() or "(no docs/discovery.md was written)"
    design_block = design.strip() or "(no docs/design.md — no design pass happened)"
    return (
        f"{opening}\n\n"
        f"<user_idea>\n{idea}\n</user_idea>\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>\n\n"
        f"<discovery>\n{discovery_block}\n</discovery>\n\n"
        f"<design>\n{design_block}\n</design>\n\n"
        f"<test_report>\n{_test_report_block(report)}\n</test_report>\n\n"
        f"<app_files>\n{_snapshot_block(snapshot)}\n</app_files>"
    )


def _messages(
    idea: str,
    spec: Spec,
    discovery: str,
    design: str,
    report: TestReport,
    snapshot: dict[str, str],
    error: str | None,
    *,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> list[dict[str, str]]:
    user = _body(
        idea,
        spec,
        discovery,
        design,
        report,
        snapshot,
        "Review this app against the original idea. Answer with the review "
        "verdict JSON only.",
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Do not explain; return corrected JSON only."
        )
    return [
        {
            "role": "system",
            "content": system_prompt_for(Role.REVIEWER, ui_product=ui_product, prod_ops=prod_ops, security=security),
        },
        {"role": "user", "content": user},
    ]


def _attempt(gateway: Gateway, messages: list[dict[str, str]]) -> ReviewVerdict:
    completion = gateway.complete(
        Role.REVIEWER,
        messages,
        response_format=response_format("review_verdict", ReviewVerdict),
        max_tokens=definition_for(Role.REVIEWER).max_output_tokens,
    )
    return ReviewVerdict.model_validate_json(completion.content)


def run(
    gateway: Gateway,
    idea: str,
    spec: Spec,
    discovery: str,
    design: str,
    report: TestReport,
    snapshot: dict[str, str],
    *,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> ReviewVerdict:
    """One honest verdict per call. One retry with the error shown."""
    try:
        return _attempt(
            gateway,
            _messages(
                idea,
                spec,
                discovery,
                design,
                report,
                snapshot,
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
                _messages(
                    idea,
                    spec,
                    discovery,
                    design,
                    report,
                    snapshot,
                    str(first),
                    ui_product=ui_product,
                    prod_ops=prod_ops,
                    security=security,
                ),
            )
        except ValidationError as second:
            raise ReviewerError(
                f"reviewer produced an invalid verdict twice: {second}"
            ) from second


async def run_agentic(
    gateway: Gateway,
    idea: str,
    spec: Spec,
    discovery: str,
    design: str,
    report: TestReport,
    snapshot: dict[str, str],
    call_tool: CallTool,
    transcript: TranscriptLog | None = None,
    agent_id: str | None = None,
    *,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> ReviewVerdict:
    """Verification pass: the reviewer reads (never writes) before it judges."""
    definition = definition_for(Role.REVIEWER)
    tools = specs_for(
        definition.tools,
        overrides={
            "submit_result": ToolSpec(
                "submit_result",
                "Finish. Call this once, when every claim in the brief has "
                "been checked against what you observed.",
                ReviewVerdict,
            )
        },
    )
    user = _body(
        idea,
        spec,
        discovery,
        design,
        report,
        snapshot,
        "Review this app against the original idea. Verify each claim with "
        "your read-only tools before you commit to it. When every claim has "
        "been checked, call submit_result with the verdict.",
    )
    try:
        outcome = await run_agent_loop(
            gateway=gateway,
            role=Role.REVIEWER,
            system=system_prompt_for(Role.REVIEWER, ui_product=ui_product, prod_ops=prod_ops, security=security),
            user=user,
            tools=tools,
            call_tool=call_tool,
            finish=ReviewVerdict,
            max_steps=definition.max_steps,
            max_tokens=definition.max_output_tokens,
            transcript=transcript,
            agent_id=agent_id,
        )
    except AgentLoopError as exc:
        raise ReviewerError(str(exc)) from exc
    return outcome.result
