"""A scripted demo run.

Plays the golden-path story: spec, plan, tests, two developers building in
parallel and talking to each other (a question and its answer, a contract
change), the tech lead settling an escalation, one failure, one fix, green.
It exists so the floor view can be built and recorded without real models. It
never calls a model and it never writes files.
"""

from collections.abc import Callable, Iterator

from kiln.agents.roles import Role
from kiln.events.schema import (
    AgentRef,
    AgentStatus,
    MessageSent,
    NoteAdded,
    RunFinished,
    RunStarted,
    TaskAssigned,
    TaskRef,
    TeamEvent,
    TeamUpdate,
    TestRunFinished,
    UsageTick,
)

REQUIREMENTS = AgentRef(id="requirements", role=Role.REQUIREMENTS, label="Riley Requirements")
PLANNER = AgentRef(id="planner", role=Role.PLANNER, label="Pat Planner")
TEST_AUTHOR = AgentRef(id="test_author", role=Role.TEST_AUTHOR, label="Tessa Tests")
WORKER_1 = AgentRef(id="worker-1", role=Role.WORKER, label="Dev A (backend)")
WORKER_2 = AgentRef(id="worker-2", role=Role.WORKER, label="Dev B (frontend)")
FIXER = AgentRef(id="fixer", role=Role.FIXER, label="Felix Fixer")
REVIEWER = AgentRef(id="reviewer", role=Role.REVIEWER, label="Remy Reviewer")
REPORTER = AgentRef(id="reporter", role=Role.REPORTER, label="Rosa Reporter")
LEAD = AgentRef(id="supervisor", role=Role.SUPERVISOR, label="Lena Lead")

DEMO_AGENTS: tuple[AgentRef, ...] = (
    REQUIREMENTS,
    PLANNER,
    TEST_AUTHOR,
    WORKER_1,
    WORKER_2,
    FIXER,
    REVIEWER,
    REPORTER,
    LEAD,
)

_CEILING = 200_000


def _usage(budget: dict[str, int]) -> Callable[[], UsageTick]:
    def tick() -> UsageTick:
        budget["used"] += 800
        return UsageTick(tokens_used=budget["used"], ceiling=_CEILING, calls=budget["calls"] + 1)

    return tick


def _team(statuses: dict[str, str], current: dict[str, str | None], **counts) -> TeamUpdate:
    titles = {"t1": "Accounts API", "t2": "Signup form", "t3": "To-do API", "t4": "To-do list UI"}
    owners = {"t1": "worker-1", "t2": "worker-2", "t3": "worker-1", "t4": "worker-2"}
    lanes = [
        {"agent_id": "worker-1", "label": WORKER_1.label, "focus": "backend",
         "current": current.get("worker-1"), "current_title": titles.get(current.get("worker-1") or "", "")},
        {"agent_id": "worker-2", "label": WORKER_2.label, "focus": "frontend",
         "current": current.get("worker-2"), "current_title": titles.get(current.get("worker-2") or "", "")},
    ]
    tasks = [
        {"id": tid, "title": titles[tid], "agent_id": owners[tid], "status": statuses.get(tid, "pending")}
        for tid in titles
    ]
    return TeamUpdate(lanes=lanes, tasks=tasks, **counts)


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
    yield MessageSent(from_agent="planner", to_agent="test_author", summary="task graph + contract ready")
    yield AgentStatus(agent=PLANNER, status="done", detail="plan posted: 4 tasks, 2 developers")
    yield _team({}, {})

    yield AgentStatus(agent=TEST_AUTHOR, status="working", detail="writing tests from the contract")
    yield tick()
    yield MessageSent(from_agent="test_author", to_agent="worker-1", summary="tests ready")
    yield MessageSent(from_agent="test_author", to_agent="worker-2", summary="tests ready")
    yield AgentStatus(agent=TEST_AUTHOR, status="done", detail="tests handed off")

    # two developers in parallel, each on their own side of the contract
    yield TaskAssigned(task=TaskRef(id="t1", title="Accounts API", owned_paths=["backend/accounts/"]),
                       agent_id="worker-1")
    yield TaskAssigned(task=TaskRef(id="t2", title="Signup form", owned_paths=["frontend/src/Signup.tsx"]),
                       agent_id="worker-2")
    yield _team({"t1": "running", "t2": "running"}, {"worker-1": "t1", "worker-2": "t2"})
    yield AgentStatus(agent=WORKER_1, status="working", detail="writing backend/accounts/")
    yield AgentStatus(agent=WORKER_2, status="working", detail="building the signup form")
    yield tick()
    yield TeamEvent(category="question", from_agent="worker-2", to_agent="worker-1",
                    text="question Q1: does POST /api/signup lowercase the email?")
    yield TeamEvent(category="answer", from_agent="worker-1", to_agent="worker-2",
                    text="answer to Q1: yes — trimmed and lowercased")
    yield TeamEvent(category="contract", from_agent="worker-1", to_agent="worker-2",
                    text="CONTRACT CHANGED — POST /api/signup: response adds display_name: string")
    yield _team({"t1": "running", "t2": "running"}, {"worker-1": "t1", "worker-2": "t2"},
                contract_changes=1)

    # a client note lands mid-build
    yield NoteAdded(text="tasks need a due-date field")
    yield MessageSent(from_agent="client", to_agent="planner", summary="due dates on tasks")

    # a product question nobody recorded goes to the tech lead
    yield TeamEvent(category="escalation", from_agent="worker-2", to_agent="supervisor",
                    text="[product] show due dates as relative ('in 2 days') or absolute?")
    yield AgentStatus(agent=LEAD, status="thinking", detail="due-date display")
    yield tick()
    yield TeamEvent(category="decision", from_agent="supervisor", to_agent="worker-2",
                    text="answer: relative under a week, absolute after — discovery says 'friendly dates'")
    yield AgentStatus(agent=LEAD, status="done", detail="answer: due-date display")

    yield AgentStatus(agent=WORKER_1, status="done", detail="Accounts API complete")
    yield TaskAssigned(task=TaskRef(id="t3", title="To-do API", owned_paths=["backend/todos/"]),
                       agent_id="worker-1")
    yield AgentStatus(agent=WORKER_2, status="done", detail="Signup form complete")
    yield TaskAssigned(task=TaskRef(id="t4", title="To-do list UI", owned_paths=["frontend/src/Todos.tsx"]),
                       agent_id="worker-2")
    yield _team({"t1": "done", "t2": "done", "t3": "running", "t4": "running"},
                {"worker-1": "t3", "worker-2": "t4"}, contract_changes=1, decisions=1)
    yield tick()
    yield AgentStatus(agent=WORKER_1, status="done", detail="To-do API complete")
    yield AgentStatus(agent=WORKER_2, status="done", detail="To-do list UI complete")
    yield _team({tid: "done" for tid in ("t1", "t2", "t3", "t4")}, {},
                contract_changes=1, decisions=1)

    yield TestRunFinished(passed=11, failed=1, skipped=0, errors=0, green=False)
    yield MessageSent(from_agent="worker-1", to_agent="fixer", summary="test_accounts_login fails")
    yield AgentStatus(agent=FIXER, status="working", detail="patching the login route")
    yield tick()
    yield AgentStatus(agent=FIXER, status="done", detail="patch applied")

    yield TestRunFinished(passed=12, failed=0, skipped=0, errors=0, green=True)

    yield AgentStatus(agent=REPORTER, status="working", detail="writing README and cost report")
    yield tick()
    yield AgentStatus(agent=REPORTER, status="done", detail="docs ready")

    yield RunFinished(
        run_id=run_id,
        green=True,
        wall_clock_seconds=0,
        notes=[
            "contract change by Dev A (backend): POST /api/signup: response adds display_name",
            "tech lead: [product] due-date display → answer",
        ],
    )
