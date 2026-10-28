"""Test author: acceptance criteria to a suite that runs before code exists.

The model's reply must validate as a `TestSuite` on every run, including
complete criterion coverage — the suite is the independent judge the workers
can never edit. A reply that fails validation is retried once with the error
shown, then counted as a failure.
"""

from pydantic import ValidationError

from kiln.agents.definitions import definition_for
from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite


class TestAuthorError(RuntimeError):
    """The test author could not produce a valid suite."""


def _messages(spec: Spec, error: str | None) -> list[dict[str, str]]:
    user = (
        "Write the pytest suite for this specification. "
        "Answer with the suite JSON only.\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>"
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: an acceptance criterion "
            "with no test covering it, a file outside tests/, or a covers list naming "
            "a criterion that does not exist. Return corrected JSON only."
        )
    return [
        {"role": "system", "content": load_prompt("test_author")},
        {"role": "user", "content": user},
    ]


def _attempt(gateway: Gateway, spec: Spec, messages: list[dict[str, str]]) -> TestSuite:
    completion = gateway.complete(
        Role.TEST_AUTHOR,
        messages,
        max_tokens=definition_for(Role.TEST_AUTHOR).max_output_tokens,
        response_format=response_format("test_suite", TestSuite),
    )
    criterion_ids = [criterion.id for criterion in spec.acceptance_criteria]
    return TestSuite.model_validate_json(
        completion.content, context={"criterion_ids": criterion_ids}
    )


def run(gateway: Gateway, spec: Spec) -> TestSuite:
    """Turn a spec into a validated suite. One retry with the error shown."""
    try:
        return _attempt(gateway, spec, _messages(spec, None))
    except ValidationError as first:
        try:
            return _attempt(gateway, spec, _messages(spec, str(first)))
        except ValidationError as second:
            raise TestAuthorError(
                f"test author produced an invalid suite twice: {second}"
            ) from second
