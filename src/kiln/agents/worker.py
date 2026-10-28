"""Dev worker: one task, written only inside the paths that task owns.

The model's reply must validate as a `WorkerResult` on every run: files only
inside the task's owned paths, never under tests/, for the task it was given.
A reply that fails validation is retried once with the error shown, then
counted as a failure.
"""

from pydantic import ValidationError

from kiln.agents.definitions import definition_for
from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Task
from kiln.schemas.work import WorkerResult

MAX_SUITE_CHARS = 6000
MAX_FILE_CHARS = 4000


class WorkerError(RuntimeError):
    """The worker could not produce a valid result."""


def _tests_block(suite: TestSuite | None) -> str:
    if suite is None:
        return "<tests>\n(no suite provided)\n</tests>"
    parts = []
    for file in suite.files:
        content = file.content[:MAX_SUITE_CHARS]
        if len(file.content) > MAX_SUITE_CHARS:
            content += "\n# ... trimmed ..."
        parts.append(f"# {file.path}\n{content}")
    return "<tests>\n" + "\n\n".join(parts) + "\n</tests>"


def _current_files_block(current_files: dict[str, str]) -> str:
    if not current_files:
        return ""
    blocks = []
    for path, content in current_files.items():
        trimmed = content[:MAX_FILE_CHARS]
        if len(content) > MAX_FILE_CHARS:
            trimmed += "\n# ... trimmed ..."
        blocks.append(f'<current_files path="{path}">\n{trimmed}\n</current_files>')
    return "\n\n" + "\n\n".join(blocks)


def _messages(
    task: Task,
    spec: Spec,
    suite: TestSuite | None,
    current_files: dict[str, str] | None,
    error: str | None,
) -> list[dict[str, str]]:
    user = (
        "Deliver your task. Answer with the worker result JSON only.\n\n"
        f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>\n\n"
        + _tests_block(suite)
        + _current_files_block(current_files or {})
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: a file outside the task's "
            "owned paths, a file under tests/, or a result for the wrong task id. "
            "Return corrected JSON only."
        )
    return [
        {"role": "system", "content": load_prompt("worker")},
        {"role": "user", "content": user},
    ]


def _attempt(
    gateway: Gateway,
    task: Task,
    messages: list[dict[str, str]],
) -> WorkerResult:
    completion = gateway.complete(
        Role.WORKER,
        messages,
        max_tokens=definition_for(Role.WORKER).max_output_tokens,
        response_format=response_format("worker_result", WorkerResult),
    )
    return WorkerResult.model_validate_json(
        completion.content,
        context={"task_id": task.id, "owned_paths": task.owned_paths},
    )


def run(
    gateway: Gateway,
    task: Task,
    spec: Spec,
    suite: TestSuite | None = None,
    current_files: dict[str, str] | None = None,
) -> WorkerResult:
    """Deliver one task as a validated worker result. One retry with the error shown."""
    try:
        return _attempt(gateway, task, _messages(task, spec, suite, current_files, None))
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                task,
                _messages(task, spec, suite, current_files, str(first)),
            )
        except ValidationError as second:
            raise WorkerError(
                f"worker produced an invalid result twice: {second}"
            ) from second
