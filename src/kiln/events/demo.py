"""A scripted demo run.

Plays the golden-path story from the demo video: spec, plan, tests, parallel
build, one failure, one fix, green. It exists so the floor view can be built
and recorded before the agent loop calls real models. It never calls a model
and it never writes files.
"""

from collections.abc import Callable, Iterator

from kiln.agents.roles import Role
from kiln.events.schema import (
    AgentRef,
    AgentStatus,
    MessageSent,
    RunFinished,
    RunStarted,
    TaskAssigned,
    TaskRef,
    TestRunFinished,
    UsageTick,
)

REQUIREMENTS = AgentRef(id="requirements", role=Role.REQUIREMENTS, label="Riley Requirements")
PLANNER = AgentRef(id="planner", role=Role.PLANNER, label="Pat Planner")
TEST_AUTHOR = AgentRef(id="test_author", role=Role.TEST_AUTHOR, label="Tessa Tests")
WORKER_1 = AgentRef(id="worker-1", role=Role.WORKER, label="Wren Worker")
WORKER_2 = AgentRef(id="worker-2", role=Role.WORKER, label="Wade Worker")
FIXER = AgentRef(id="fixer", role=Role.FIXER, label="Felix Fixer")
REPORTER = AgentRef(id="reporter", role=Role.REPORTER, label="Rosa Reporter")

DEMO_AGENTS: tuple[AgentRef, ...] = (
    REQUIREMENTS,
    PLANNER,
    TEST_AUTHOR,
    WORKER_1,
    WORKER_2,
    FIXER,
    REPORTER,
)

_CEILING = 200_000


def _usage(budget: dict[str, int]) -> Callable[[], UsageTick]:
    def tick() -> UsageTick:
        budget["used"] += 800
        return UsageTick(tokens_used=budget["used"], ceiling=_CEILING, calls=budget["calls"] + 1)

    return tick


def script_demo_run(run_id: str = "demo-1", idea: str = "A to-do app with accounts") -> Iterator:
    """Yield FloorEvents with small delays. Pure generator; timing is the caller's job."""

    budget = {"used": 0, "calls": 0}
    tick = _usage(budget)

    yield RunStarted(run_id=run_id, idea=idea)

    yield AgentStatus(agent=REQUIREMENTS, status="thinking", detail="reading the idea")
    yield AgentStatus(agent=REQUIREMENTS, status="working", detail="writing spec.md")
    yield tick()
    yield MessageSent(from_agent="requirements", to_agent="planner", summary="spec.md ready")
    yield AgentStatus(agent=REQUIREMENTS, status="done", detail="spec approved")

    yield AgentStatus(agent=PLANNER, status="thinking", detail="splitting the spec into tasks")
    yield tick()
    yield TaskAssigned(
        task=TaskRef(id="t1", title="Accounts API", owned_paths=["app/accounts/"]),
        agent_id="worker-1",
    )
    yield TaskAssigned(
        task=TaskRef(id="t2", title="To-do API", owned_paths=["app/todos/"]),
        agent_id="worker-2",
    )
    yield MessageSent(from_agent="planner", to_agent="test_author", summary="task graph ready")
    yield AgentStatus(agent=PLANNER, status="done", detail="plan posted")

    yield AgentStatus(agent=TEST_AUTHOR, status="working", detail="writing tests from the spec")
    yield tick()
    yield MessageSent(from_agent="test_author", to_agent="worker-1", summary="tests ready")
    yield MessageSent(from_agent="test_author", to_agent="worker-2", summary="tests ready")
    yield AgentStatus(agent=TEST_AUTHOR, status="done", detail="tests handed off")

    yield AgentStatus(agent=WORKER_1, status="working", detail="writing app/accounts/")
    yield AgentStatus(agent=WORKER_2, status="working", detail="writing app/todos/")
    yield tick()
    yield tick()

    yield AgentStatus(agent=WORKER_2, status="done", detail="app/todos/ complete")
    yield AgentStatus(agent=WORKER_1, status="done", detail="app/accounts/ complete")

    yield TestRunFinished(passed=11, failed=1, skipped=0, errors=0, green=False)
    yield MessageSent(from_agent="worker-1", to_agent="fixer", summary="test_accounts_login fails")
    yield AgentStatus(agent=FIXER, status="working", detail="patching the login route")
    yield tick()
    yield AgentStatus(agent=FIXER, status="done", detail="patch applied")

    yield TestRunFinished(passed=12, failed=0, skipped=0, errors=0, green=True)

    yield AgentStatus(agent=REPORTER, status="working", detail="writing README and cost report")
    yield tick()
    yield AgentStatus(agent=REPORTER, status="done", detail="docs ready")

    yield RunFinished(run_id=run_id, green=True, wall_clock_seconds=0)
