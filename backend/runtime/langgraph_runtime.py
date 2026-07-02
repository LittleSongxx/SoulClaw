"""LangGraph topology used by SoulClaw's turn runtime."""

from __future__ import annotations

from typing import Any

from backend.domain.runs import GRAPH_NODE_ORDER


class AgentTurnGraphShell:
    """Compiled LangGraph topology for the turn graph.

    Node side effects are still orchestrated by ``AgentRuntime`` so the public
    turn/resume contract stays stable, but topology, run graph metadata, and
    Postgres checkpoints now share one canonical node order.
    """

    def __init__(self) -> None:
        self.available = False
        self.error = ""
        self.graph: Any | None = None
        try:
            from langgraph.graph import END, StateGraph

            graph = StateGraph(dict)
            for node_name in GRAPH_NODE_ORDER:
                graph.add_node(node_name, _identity_node)
            graph.set_entry_point(GRAPH_NODE_ORDER[0])
            for source, target in zip(GRAPH_NODE_ORDER, GRAPH_NODE_ORDER[1:], strict=False):
                graph.add_edge(source, target)
            graph.add_edge(GRAPH_NODE_ORDER[-1], END)
            self.graph = graph.compile()
            self.available = True
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)

    def state(self) -> dict[str, Any]:
        return {"available": self.available, "error": self.error, "nodes": list(GRAPH_NODE_ORDER)}


def _identity_node(state: dict[str, Any]) -> dict[str, Any]:
    return state
