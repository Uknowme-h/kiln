"""Reporter: run artifacts to a README draft, run summary, and cost report.

Two modes:
- one-shot structured JSON (legacy / tests): the model replies with a bundle;
- agentic pass: read-only tools (read_file, glob, grep) let it verify claims
  against the workspace — seams, stubs, unimplemented contract routes — before
  it commits them to the documents.

The model's reply must validate as a `ReportBundle` on every run. A reply
that fails validation is retried once with the error shown, then counted as
a failure.
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
from kiln.schemas.report import ReportBundle
from kiln.schemas.results import TestReport
from kiln.schemas.review import ReviewVerdict
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import TaskGraph
from kiln.schemas.work import WorkerResult
from kiln.tools.registry import ToolSpec, specs_for

CallTool = Callable[[str, dict], Awaitable[str]]


class ReporterError(RuntimeError):
    """The reporter could not produce a valid bundle."""


def _test_report_block(report: TestReport) -> str:
    return (
        f"passed={report.passed} failed={report.failed} "
        f"errors={report.errors} skipped={report.skipped} green={report.green}"
    )


def _workers_block(worker_results: list[WorkerResult]) -> str:
    if not worker_results:
        return "(no worker results)"
    parts = []
    for result in worker_results:
        gaps = "; ".join(result.known_gaps) if result.known_gaps else "none"
        files = ", ".join(file.path for file in result.files)
        parts.append(
            f"- {result.task_id}: {result.summary}\n"
            f"  files: {files}\n"
            f"  what_works: {result.what_works}\n"
            f"  known_gaps: {gaps}"
        )
    return "\n".join(parts)


def _review_block(review: ReviewVerdict | None) -> str:
    """What the brief reviewer found — or why it did not run."""
    if review is None:
        return "(skipped — the suite never went green, so the brief review did not run)"
    if not review.gaps:
        return f"clear — {review.summary}"
    lines = [f"- {gap.claim} — observed: {gap.evidence}" for gap in review.gaps]
    return "\n".join(lines)


def _body(
    spec: Spec,
    tasks: TaskGraph,
    worker_results: list[WorkerResult],
    report: TestReport,
    usage: dict,
    opening: str,
    review: ReviewVerdict | None = None,
) -> str:
    return (
        f"{opening}\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>\n\n"
        f"<tasks>\n{tasks.model_dump_json(indent=2)}\n</tasks>\n\n"
        f"<worker_results>\n{_workers_block(worker_results)}\n</worker_results>\n\n"
        f"<test_report>\n{_test_report_block(report)}\n</test_report>\n\n"
        f"<review>\n{_review_block(review)}\n</review>\n\n"
        f"<usage>\n{usage}\n</usage>"
    )


def _messages(
    spec: Spec,
    tasks: TaskGraph,
    worker_results: list[WorkerResult],
    report: TestReport,
    usage: dict,
    error: str | None,
    review: ReviewVerdict | None = None,
) -> list[dict[str, str]]:
    user = _body(
        spec,
        tasks,
        worker_results,
        report,
        usage,
        "Write the README, the run summary, and the cost report for this run. "
        "Answer with the bundle JSON only.",
        review=review,
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Return corrected JSON only."
        )
    return [
        {"role": "system", "content": system_prompt_for(Role.REPORTER)},
        {"role": "user", "content": user},
    ]


def _attempt(gateway: Gateway, messages: list[dict[str, str]]) -> ReportBundle:
    completion = gateway.complete(
        Role.REPORTER,
        messages,
        max_tokens=definition_for(Role.REPORTER).max_output_tokens,
        response_format=response_format("report_bundle", ReportBundle),
    )
    return ReportBundle.model_validate_json(completion.content)


def run(
    gateway: Gateway,
    spec: Spec,
    tasks: TaskGraph,
    worker_results: list[WorkerResult],
    report: TestReport,
    usage: dict,
    review: ReviewVerdict | None = None,
) -> ReportBundle:
    """Turn run artifacts into a validated bundle. One retry with the error shown."""
    try:
        return _attempt(
            gateway, _messages(spec, tasks, worker_results, report, usage, None, review)
        )
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                _messages(spec, tasks, worker_results, report, usage, str(first), review),
            )
        except ValidationError as second:
            raise ReporterError(
                f"reporter produced an invalid bundle twice: {second}"
            ) from second


async def run_agentic(
    gateway: Gateway,
    spec: Spec,
    tasks: TaskGraph,
    worker_results: list[WorkerResult],
    report: TestReport,
    usage: dict,
    call_tool: CallTool,
    transcript: TranscriptLog | None = None,
    agent_id: str | None = None,
    review: ReviewVerdict | None = None,
) -> ReportBundle:
    """Verification pass: the reporter reads (never writes) before it claims.

    The documents themselves are written by the orchestrator under its own
    scope after this bundle validates — the loop only gathers evidence.
    """
    definition = definition_for(Role.REPORTER)
    tools = specs_for(
        definition.tools,
        overrides={
            "submit_result": ToolSpec(
                "submit_result",
                "Finish. Call this once, when the README, run summary, and cost "
                "report are drafted from what you actually verified.",
                ReportBundle,
            )
        },
    )
    user = _body(
        spec,
        tasks,
        worker_results,
        report,
        usage,
        "Write the README, the run summary, and the cost report for this run. "
        "Before committing a claim, verify it with your read-only tools "
        "(read_file, glob, grep): check the files and tests you cite, and hunt "
        "for seams — duplicate helpers, TODO/FIXME/stub markers left by "
        "handoffs, contract routes with no implementation. When the three "
        "documents are ready, call submit_result with the bundle.",
        review=review,
    )
    settings = gateway.settings
    try:
        outcome = await run_agent_loop(
            gateway=gateway,
            role=Role.REPORTER,
            system=system_prompt_for(Role.REPORTER),
            user=user,
            tools=tools,
            call_tool=call_tool,
            finish=ReportBundle,
            max_steps=definition.max_steps,
            max_tokens=definition.max_output_tokens,
            transcript=transcript,
            agent_id=agent_id,
            # share the one context policy with the coding loops — the reporter's
            # read-only seam scan reads many files before drafting
            enable_compaction=settings.enable_compaction,
            compact_threshold_chars=settings.compact_threshold_chars,
            prune_protect_chars=settings.prune_protect_chars,
        )
    except AgentLoopError as exc:
        raise ReporterError(str(exc)) from exc
    return outcome.result
