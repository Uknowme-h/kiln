"""Reporter: run artifacts to a README draft, run summary, and cost report.

The model's reply must validate as a `ReportBundle` on every run. A reply
that fails validation is retried once with the error shown, then counted as
a failure.
"""

from pydantic import ValidationError

from kiln.agents.definitions import definition_for
from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.report import ReportBundle
from kiln.schemas.results import TestReport
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import TaskGraph
from kiln.schemas.work import WorkerResult


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


def _messages(
    spec: Spec,
    tasks: TaskGraph,
    worker_results: list[WorkerResult],
    report: TestReport,
    usage: dict,
    error: str | None,
) -> list[dict[str, str]]:
    user = (
        "Write the README, the run summary, and the cost report for this run. "
        "Answer with the bundle JSON only.\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>\n\n"
        f"<tasks>\n{tasks.model_dump_json(indent=2)}\n</tasks>\n\n"
        f"<worker_results>\n{_workers_block(worker_results)}\n</worker_results>\n\n"
        f"<test_report>\n{_test_report_block(report)}\n</test_report>\n\n"
        f"<usage>\n{usage}\n</usage>"
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Return corrected JSON only."
        )
    return [
        {"role": "system", "content": load_prompt("reporter")},
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
) -> ReportBundle:
    """Turn run artifacts into a validated bundle. One retry with the error shown."""
    try:
        return _attempt(gateway, _messages(spec, tasks, worker_results, report, usage, None))
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                _messages(spec, tasks, worker_results, report, usage, str(first)),
            )
        except ValidationError as second:
            raise ReporterError(
                f"reporter produced an invalid bundle twice: {second}"
            ) from second
