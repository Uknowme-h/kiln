"""LangGraph pipeline. Stub nodes record their phase; agent bodies replace
them one at a time. Requirements and planner are real when their callables
are provided and stubbed otherwise, so the graph still walks every phase in
tests. The graph import stays behind this module so a plain state machine can
take its place.
"""

from collections.abc import Callable

from kiln.events.log import EventLog
from kiln.graph.state import NODE_ORDER, RunState
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import TaskGraph

Analyst = Callable[[str, tuple[str, ...]], Spec]
Planner = Callable[[Spec, tuple[str, ...]], TaskGraph]
TestAuthor = Callable[[Spec], TestSuite]


def build_pipeline(
    events: EventLog | None = None,
    analyst: Analyst | None = None,
    planner: Planner | None = None,
    test_author: TestAuthor | None = None,
):
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(RunState)
    bodies = {"requirements": analyst, "planner": planner, "test_author": test_author}

    def make_node(phase: str):
        body = bodies.get(phase)

        def node(state: RunState) -> dict:
            if not state.get("idea"):
                raise ValueError("a run needs an idea")
            if events is not None:
                events.append(phase, f"entered {phase}")
            if body is None:
                return {"phase": phase}
            if phase == "requirements":
                spec = body(state["idea"], tuple(state.get("user_notes", [])))
                return {"phase": phase, "spec": spec}
            if phase == "planner":
                if "spec" not in state:
                    raise ValueError("the planner needs a spec from the requirements phase")
                tasks = body(state["spec"], tuple(state.get("user_notes", [])))
                return {"phase": phase, "tasks": tasks}
            if phase == "test_author":
                if "spec" not in state:
                    raise ValueError("the test author needs a spec from the requirements phase")
                tests = body(state["spec"])
                return {"phase": phase, "tests": tests}
            return {"phase": phase}

        return node

    for name in NODE_ORDER:
        graph.add_node(name, make_node(name))

    graph.add_edge(START, NODE_ORDER[0])
    for current, nxt in zip(NODE_ORDER, NODE_ORDER[1:]):
        graph.add_edge(current, nxt)
    graph.add_edge(NODE_ORDER[-1], END)
    return graph.compile()
