"""Typed run state and the order of the pipeline."""

from typing import TypedDict

from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import TaskGraph


class RunState(TypedDict, total=False):
    idea: str
    phase: str
    spec: Spec
    tasks: TaskGraph
    tests: TestSuite
    user_notes: list[str]


# The fixer is a loop on the runner, added when test execution exists.
NODE_ORDER = (
    "requirements",
    "planner",
    "test_author",
    "workers",
    "runner",
    "reporter",
)
