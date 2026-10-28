"""Strict JSON schema for structured output.

The Model API's strict mode requires, on every object in the schema:
``additionalProperties: false`` and a ``required`` list naming every property
(optionality is expressed with nullable types, not omission). Pydantic does
not emit either by default, so every schema sent to the API goes through this
conversion first.
"""

from typing import Any

from pydantic import BaseModel


def _enforce(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
            properties = node.get("properties")
            if isinstance(properties, dict):
                required = set(node.get("required", []))
                node["required"] = sorted(required | set(properties))
        for value in node.values():
            _enforce(value)
    elif isinstance(node, list):
        for item in node:
            _enforce(item)


def strict_json_schema(model: type[BaseModel]) -> dict:
    """The model's JSON schema with strict-mode object rules applied."""
    schema = model.model_json_schema()
    _enforce(schema)
    return schema


def response_format(name: str, model: type[BaseModel]) -> dict:
    """A ready-to-send ``response_format`` payload for a pydantic model."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": strict_json_schema(model)},
    }
