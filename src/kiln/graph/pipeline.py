"""LangGraph pipeline. Each node only records which phase it entered.

Agent bodies replace these nodes one at a time. The graph import stays behind
this module so a plain state machine can take its place.
"""

from kiln.events.log import EventLog
from kiln.graph.state import NODE_ORDER, RunState


def build_pipeline(events: EventLog | None = None):
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(RunState)

    def make_node(phase: str):
        def node(state: RunState) -> dict[str, str]:
            if not state.get("idea"):
                raise ValueError("a run needs an idea")
            if events is not None:
                events.append(phase, f"entered {phase}")
            return {"phase": phase}

        return node

    for name in NODE_ORDER:
        graph.add_node(name, make_node(name))

    graph.add_edge(START, NODE_ORDER[0])
    for current, nxt in zip(NODE_ORDER, NODE_ORDER[1:]):
        graph.add_edge(current, nxt)
    graph.add_edge(NODE_ORDER[-1], END)
    return graph.compile()
