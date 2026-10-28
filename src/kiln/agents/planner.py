"""PM planner: spec to a task graph with dependencies and file ownership.

The model's reply must validate as a `TaskGraph` on every run — unique ids,
valid dependencies, no cycles, no overlapping owned paths. A reply that fails
validation is retried once with the error shown, then counted as a failure.
"""

from pydantic import ValidationError

from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import TaskGraph


class PlannerError(RuntimeError):
    """The planner could not produce a valid task graph."""


def _messages(spec: Spec, user_notes: tuple[str, ...], error: str | None) -> list[dict[str, str]]:
    spec_block = f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>"
    if user_notes:
        notes = "\n".join(f"- {note}" for note in user_notes)
        spec_block += f"\n\n<user_notes>\n{notes}\n</user_notes>"
    user = (
        "Plan this spec into a task graph. Answer with the task graph JSON only.\n\n"
        + spec_block
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: overlapping owned paths, "
            "a dependency cycle, or a task with no owned paths. Return corrected JSON only."
        )
    return [
        {"role": "system", "content": load_prompt("planner")},
        {"role": "user", "content": user},
    ]


def _attempt(gateway: Gateway, messages: list[dict[str, str]]) -> TaskGraph:
    completion = gateway.complete(
        Role.PLANNER,
        messages,
        response_format=response_format("task_graph", TaskGraph),
    )
    return TaskGraph.model_validate_json(completion.content)


def run(gateway: Gateway, spec: Spec, user_notes: tuple[str, ...] = ()) -> TaskGraph:
    """Turn a spec into a validated task graph. One retry with the error shown."""
    try:
        return _attempt(gateway, _messages(spec, user_notes, None))
    except ValidationError as first:
        try:
            return _attempt(gateway, _messages(spec, user_notes, str(first)))
        except ValidationError as second:
            raise PlannerError(
                f"planner produced an invalid task graph twice: {second}"
            ) from second
