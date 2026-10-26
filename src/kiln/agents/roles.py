"""Agent roles. Model choice lives in the gateway, not in each agent."""

from enum import StrEnum


class Role(StrEnum):
    REQUIREMENTS = "requirements"
    PLANNER = "planner"
    TEST_AUTHOR = "test_author"
    WORKER = "worker"
    FIXER = "fixer"
    REPORTER = "reporter"


# Planning roles stay on Muse Spark. Coding roles prefer Muse Spark Open.
SPARK_ROLES = frozenset({Role.REQUIREMENTS, Role.PLANNER, Role.TEST_AUTHOR})
OPEN_WEIGHT_ROLES = frozenset({Role.WORKER, Role.FIXER, Role.REPORTER})
