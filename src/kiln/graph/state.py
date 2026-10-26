"""Typed run state and the order of the pipeline. Nodes do not call models yet."""

from typing import TypedDict


class RunState(TypedDict, total=False):
    idea: str
    phase: str


# The fixer is a loop on the runner, added when test execution exists.
NODE_ORDER = (
    "requirements",
    "planner",
    "test_author",
    "workers",
    "runner",
    "reporter",
)
