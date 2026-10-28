"""Requirements analyst: idea text to a typed spec.

The model's reply must validate as a `Spec` on every run. A reply that fails
validation is retried once with the error shown, then counted as a failure.
On ambiguity the analyst proceeds on stated assumptions; the user is not here
to answer questions.
"""

from pydantic import ValidationError

from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec


class RequirementsError(RuntimeError):
    """The analyst could not produce a valid spec."""


def _messages(idea: str, user_notes: tuple[str, ...], error: str | None) -> list[dict[str, str]]:
    idea_block = f"<user_idea>\n{idea}\n</user_idea>"
    if user_notes:
        notes = "\n".join(f"- {note}" for note in user_notes)
        idea_block += f"\n\n<user_notes>\n{notes}\n</user_notes>"
    user = (
        "Write the specification for this product idea. Answer with the spec JSON only.\n\n"
        + idea_block
    )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Do not explain; return corrected JSON only."
        )
    return [
        {"role": "system", "content": load_prompt("requirements")},
        {"role": "user", "content": user},
    ]


def _attempt(gateway: Gateway, messages: list[dict[str, str]]) -> Spec:
    completion = gateway.complete(
        Role.REQUIREMENTS,
        messages,
        response_format=response_format("spec", Spec),
    )
    return Spec.model_validate_json(completion.content)


def run(gateway: Gateway, idea: str, user_notes: tuple[str, ...] = ()) -> Spec:
    """Turn an idea into a validated spec. One retry with the error shown."""
    if not idea.strip():
        raise RequirementsError("a run needs an idea")
    try:
        return _attempt(gateway, _messages(idea, user_notes, None))
    except ValidationError as first:
        try:
            return _attempt(gateway, _messages(idea, user_notes, str(first)))
        except ValidationError as second:
            raise RequirementsError(
                f"requirements analyst produced an invalid spec twice: {second}"
            ) from second
