"""Test author: acceptance criteria to a suite that runs before code exists.

The model's reply must validate as a `TestSuite` on every run, including
complete criterion coverage — the suite is the independent judge the workers
can never edit. A reply that fails validation is retried once with the error
shown, then counted as a failure.
"""

from pydantic import ValidationError

from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite


class TestAuthorError(RuntimeError):
    """The test author could not produce a valid suite."""


def _system_prompt(spec: Spec, *, ui_product: bool = False, prod_ops: bool = False, security: bool = False) -> str:
    base = system_prompt_for(Role.TEST_AUTHOR, ui_product=ui_product, prod_ops=prod_ops, security=security)
    if getattr(spec, "stack_id", "python") == "node":
        try:
            return base + "\n\n" + load_prompt("test_author_node")
        except FileNotFoundError:
            return base
    return base


def _messages(
    spec: Spec, error: str | None, *, ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False
) -> list[dict[str, str]]:
    runner = "vitest" if getattr(spec, "stack_id", "python") == "node" else "pytest"
    user = (
        f"Write the {runner} suite for this specification. "
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
        {"role": "system", "content": _system_prompt(spec, ui_product=ui_product, prod_ops=prod_ops, security=security)},
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


def run(gateway: Gateway, spec: Spec, *, ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False) -> TestSuite:
    """Turn a spec into a validated suite. One retry with the error shown."""
    try:
        return _attempt(gateway, spec, _messages(spec, None, ui_product=ui_product, prod_ops=prod_ops, security=security))
    except ValidationError as first:
        try:
            return _attempt(
                gateway, spec, _messages(spec, str(first), ui_product=ui_product, prod_ops=prod_ops, security=security)
            )
        except ValidationError as second:
            raise TestAuthorError(
                f"test author produced an invalid suite twice: {second}"
            ) from second
