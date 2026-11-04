"""Test author: acceptance criteria to a suite that runs before code exists.

The model's reply must validate as a `TestSuite` on every run, including
complete criterion coverage — the suite is the independent judge the workers
can never edit. A reply that fails validation is retried once with the error
shown, then counted as a failure.

The suite judges exactly the criteria this run builds: criteria the planner
deferred are not shown as work and must not be covered (a test for a deferred
criterion would fail forever and drag the fixer into out-of-scope work). The
planner's contract is shown so tests target the routes, modules and element
ids the workers are told to build.
"""

from pydantic import ValidationError

from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Contract


class TestAuthorError(RuntimeError):
    """The test author could not produce a valid suite."""


def _system_prompt(spec: Spec, *, ui_product: bool = False, prod_ops: bool = False, security: bool = False, extra_skills: tuple[str, ...] = ()) -> str:
    base = system_prompt_for(Role.TEST_AUTHOR, ui_product=ui_product, prod_ops=prod_ops, security=security, extra_skills=extra_skills)
    if getattr(spec, "stack_id", "python") == "node":
        try:
            return base + "\n\n" + load_prompt("test_author_node")
        except FileNotFoundError:
            return base
    return base


def _scoped(spec: Spec, in_scope: list[str] | None) -> tuple[Spec, list[str]]:
    """The spec as this run builds it, plus the criterion ids left out of it."""
    if in_scope is None:
        return spec, []
    keep = set(in_scope)
    criteria = [c for c in spec.acceptance_criteria if c.id in keep]
    deferred = [c.id for c in spec.acceptance_criteria if c.id not in keep]
    if not deferred:
        return spec, []
    return spec.model_copy(update={"acceptance_criteria": criteria}), deferred


def _messages(
    spec: Spec, error: str | None, *, ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
    contract: Contract | None = None,
    deferred: list[str] | None = None,
) -> list[dict[str, str]]:
    runner = "vitest" if getattr(spec, "stack_id", "python") == "node" else "pytest"
    user = (
        f"Write the {runner} suite for this specification. "
        "Answer with the suite JSON only.\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>"
    )
    if contract is not None:
        user += (
            "\n\n<contract>\n"
            f"{contract.model_dump_json(indent=2)}\n"
            "</contract>\n"
            "The workers build exactly this contract. Assert against its routes, "
            "module signatures and element ids; where an oracle and the contract "
            "name the same thing differently, follow the contract."
        )
    if deferred:
        user += (
            "\n\n<deferred>\n"
            f"{', '.join(deferred)}\n"
            "</deferred>\n"
            "These criteria were deferred to a later run and are not in the spec "
            "above. Do not write tests for them."
        )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: an acceptance criterion "
            "with no test covering it, a file outside tests/, or a covers list naming "
            "a criterion that does not exist (or was deferred). Return corrected JSON only."
        )
    return [
        {"role": "system", "content": _system_prompt(spec, ui_product=ui_product, prod_ops=prod_ops, security=security, extra_skills=extra_skills)},
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
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
    contract: Contract | None = None,
    in_scope: list[str] | None = None) -> TestSuite:
    """Turn a spec into a validated suite. One retry with the error shown.

    ``in_scope`` is the criterion ids this run builds (None = all of them).
    """
    scoped, deferred = _scoped(spec, in_scope)
    kwargs = dict(
        ui_product=ui_product,
        prod_ops=prod_ops,
        security=security,
        extra_skills=extra_skills,
        contract=contract,
        deferred=deferred,
    )
    try:
        return _attempt(gateway, scoped, _messages(scoped, None, **kwargs))
    except ValidationError as first:
        try:
            return _attempt(gateway, scoped, _messages(scoped, str(first), **kwargs))
        except ValidationError as second:
            raise TestAuthorError(
                f"test author produced an invalid suite twice: {second}"
            ) from second
