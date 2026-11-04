"""Agent roles. Model choice lives in the gateway, not in each agent."""

from enum import StrEnum


class Role(StrEnum):
    REQUIREMENTS = "requirements"
    PLANNER = "planner"
    TEST_AUTHOR = "test_author"
    WORKER = "worker"
    FIXER = "fixer"
    REVIEWER = "reviewer"
    REPORTER = "reporter"
    SUPERVISOR = "supervisor"


# Planning roles stay on Muse Spark. Coding roles prefer Muse Spark Open.
# The reviewer judges against the brief and the tech lead makes judgment calls
# mid-build — reasoning tasks, so both ride Spark.
SPARK_ROLES = frozenset(
    {Role.REQUIREMENTS, Role.PLANNER, Role.TEST_AUTHOR, Role.REVIEWER, Role.SUPERVISOR}
)
OPEN_WEIGHT_ROLES = frozenset({Role.WORKER, Role.FIXER, Role.REPORTER})
