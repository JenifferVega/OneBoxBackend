"""Construcción del grafo del agente (LangGraph StateGraph).

Cableado:
    START → context_resolver → planner
    planner --(status=="direct")--> narrator ; sino → executor
    executor → validator
    validator --(status=="replan")--> planner ; sino → narrator
    narrator → END

Los routers son funciones puras que leen `status` (la lógica de transición
queda separada de la lógica de los nodos, como en la arquitectura de referencia).
"""
from functools import partial

from langgraph.graph import END, START, StateGraph

from agent.graph.nodes import (
    context_resolver_node, executor_node, narrator_node,
    planner_node, validator_node,
)
from agent.graph.state import AgentState


def _route_after_planner(state: AgentState) -> str:
    return "narrator" if state.get("status") == "direct" else "executor"


def _route_after_validator(state: AgentState) -> str:
    return "planner" if state.get("status") == "replan" else "narrator"


def build_graph(llms: dict):
    """Compila el grafo con los LLMs por nodo inyectados (testeable con stubs)."""
    g = StateGraph(AgentState)

    g.add_node("context_resolver", partial(context_resolver_node, llm=llms["context_resolver"]))
    g.add_node("planner", partial(planner_node, llm=llms["planner"]))
    g.add_node("executor", executor_node)  # sin LLM
    g.add_node("validator", partial(validator_node, llm=llms["validator"]))
    g.add_node("narrator", partial(narrator_node, llm=llms["narrator"]))

    g.add_edge(START, "context_resolver")
    g.add_edge("context_resolver", "planner")
    g.add_conditional_edges("planner", _route_after_planner,
                            {"executor": "executor", "narrator": "narrator"})
    g.add_edge("executor", "validator")
    g.add_conditional_edges("validator", _route_after_validator,
                            {"planner": "planner", "narrator": "narrator"})
    g.add_edge("narrator", END)

    return g.compile()
