"""Fixer: a trimmed failing report and the owned code, returned as a patch.

The model's reply must validate as a `FixPatch` on every run, including the
rule that a patch only touches the failing task's owned paths. A reply that
fails validation is retried once with the error shown, then counted as a
failure — the retry cap itself lives with the runner, which escalates to the
planner; the fixer is one honest shot per call.
"""

from pydantic import ValidationError

from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.patch import FixPatch
from kiln.schemas.results import TestReport
from kiln.schemas.tasks import Task

MAX_LOG_CHARS = 6000
MAX_FILE_CHARS = 4000


class FixerError(RuntimeError):
    """The fixer could not produce a valid patch."""


def _trimmed_cases(report: TestReport) -> str:
    failing = [case for case in report.cases if case.outcome in ("failed", "error")]
    lines = []
    for case in failing[:10]:
        message = case.message[:400] if case.message else ""
        lines.append(f"- {case.nodeid} [{case.outcome}] {message}")
    return "\n".join(lines) or "(no per-case detail)"


def _current_files_block(current_files: dict[str, str]) -> str:
    blocks = []
    for path, content in current_files.items():
        trimmed = content[:MAX_FILE_CHARS]
        if len(content) > MAX_FILE_CHARS:
            trimmed += "\n# ... trimmed ..."
        blocks.append(f"<current_files path=\"{path}\">\n{trimmed}\n</current_files>")
    return "\n\n".join(blocks)


def _messages(
    task: Task,
    report: TestReport,
    current_files: dict[str, str],
    error: str | None,
) -> list[dict[str, str]]:
    log = report.raw_log[:MAX_LOG_CHARS]
    user = (
        "Patch the code so these tests pass. Answer with the patch JSON only.\n\n"
        f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n\n"
        f"<test_report>\n{_trimmed_cases(report)}\n\nraw log (trimmed):\n{log}\n</test_report>\n\n"
        + _current_files_block(current_files)
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: a file outside the task's "
            "owned paths, or an empty diagnosis. Return corrected JSON only."
        )
    return [
        {"role": "system", "content": load_prompt("fixer")},
        {"role": "user", "content": user},
    ]


def _attempt(
    gateway: Gateway,
    task: Task,
    messages: list[dict[str, str]],
) -> FixPatch:
    completion = gateway.complete(
        Role.FIXER,
        messages,
        response_format=response_format("fix_patch", FixPatch),
    )
    return FixPatch.model_validate_json(
        completion.content, context={"owned_paths": task.owned_paths}
    )


def run(
    gateway: Gateway,
    task: Task,
    report: TestReport,
    current_files: dict[str, str],
) -> FixPatch:
    """Turn one failing report into a validated patch. One retry with the error shown."""
    if not current_files:
        raise FixerError("the fixer needs the current contents of the task's files")
    try:
        return _attempt(gateway, task, _messages(task, report, current_files, None))
    except ValidationError as first:
        try:
            return _attempt(gateway, task, _messages(task, report, current_files, str(first)))
        except ValidationError as second:
            raise FixerError(
                f"fixer produced an invalid patch twice: {second}"
            ) from second
