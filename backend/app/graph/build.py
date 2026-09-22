"""Graph assembly.

    guard
      |
    cache_lookup ---- hit ----------------------------------> END
      |  miss
    route
      |--- chitchat / out_of_scope ------------> refuse ----> END
      |--- underspecified ----------------> clarify --------> END
      |  policy_lookup / calculation / comparison
    translate
      |
    retrieve            (SQL: hybrid search -> RRF -> MMR -> expand)
      |--- nothing above threshold -----------> refuse ----> END
    rerank              (Opus 5, listwise)
      |--- nothing above threshold -----------> refuse ----> END
    generate            (Opus 5, streaming, cited)
      |
    verify              (structural citation check -- no LLM call)
      |
     END

The shape is a cost decision as much as a correctness one. Every early exit
avoids the two Opus 5 calls that dominate per-query spend, and the branches
that exit early are exactly the ones where those calls would have produced the
least value: chitchat, out-of-scope questions, and questions where retrieval
found nothing worth reasoning over.

``verify`` is deliberately not an LLM call. Checking that each factual sentence
carries a citation marker pointing at a real retrieved chunk is a regex and a
set membership test -- free, deterministic, and it catches the specific failure
that matters (an invented citation). An LLM groundedness judge would roughly
double per-query cost to answer a question arithmetic already answers.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import Nodes
from app.graph.state import GraphState

# Intents that never justify the retrieval-rerank-generate path.
_SHORT_CIRCUIT = {"chitchat", "out_of_scope"}


def route_after_cache(state: GraphState) -> str:
    return "hit" if state.get("cache_hit", "none") != "none" else "miss"


def route_after_routing(state: GraphState) -> str:
    intent = state.get("intent", "policy_lookup")
    if intent == "chitchat":
        return "refuse"
    if intent == "underspecified" and state.get("missing_facts"):
        return "clarify"
    # out_of_scope still retrieves. Citing the section that points the user at
    # the right policy is far more useful than a bare "not covered", and the
    # retrieval is cheap next to the generation it may still avoid.
    return "translate"


def route_after_retrieve(state: GraphState) -> str:
    return "rerank" if state.get("candidates") else "refuse"


def route_after_rerank(state: GraphState) -> str:
    return "generate" if state.get("reranked") else "refuse"


def build_graph(nodes: Nodes, checkpointer: Any | None = None):
    """Wire the graph. Nodes are injected so tests can stub the providers."""
    g = StateGraph(GraphState)

    g.add_node("guard", nodes.guard)
    g.add_node("cache_lookup", nodes.cache_lookup)
    g.add_node("route", nodes.route)
    g.add_node("translate", nodes.translate)
    g.add_node("retrieve", nodes.retrieve)
    g.add_node("rerank", nodes.rerank)
    g.add_node("generate", nodes.generate)
    g.add_node("verify", nodes.verify)
    g.add_node("refuse", nodes.refuse)
    g.add_node("clarify", nodes.clarify)

    g.add_edge(START, "guard")
    g.add_edge("guard", "cache_lookup")

    g.add_conditional_edges(
        "cache_lookup", route_after_cache, {"hit": END, "miss": "route"}
    )
    g.add_conditional_edges(
        "route",
        route_after_routing,
        {"translate": "translate", "refuse": "refuse", "clarify": "clarify"},
    )
    g.add_edge("translate", "retrieve")
    g.add_conditional_edges(
        "retrieve", route_after_retrieve, {"rerank": "rerank", "refuse": "refuse"}
    )
    g.add_conditional_edges(
        "rerank", route_after_rerank, {"generate": "generate", "refuse": "refuse"}
    )
    g.add_edge("generate", "verify")
    g.add_edge("verify", END)
    g.add_edge("refuse", END)
    g.add_edge("clarify", END)

    return g.compile(checkpointer=checkpointer)
