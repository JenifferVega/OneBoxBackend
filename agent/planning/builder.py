"""Wiring of the planning graph.

    START -> load_project -+- chat ------> interpret_request -+- question --> analyze_plan -> answer -> END
                           |                                  +- change/feedback -> extract_facts -+
                           |                                  +- ingest -> load_sources ------------+
                           |                                  +- decide -> apply -> END   (answer_question -> reconcile)
                           +- document/reanalyze -> load_sources -> extract_facts -> reconcile
                           +- cron/task_event ---> analyze_plan --(incongruences)--> reconcile | END

    reconcile -> link_dependencies -> validate --(errors, < 3 rounds)--> reconcile
                                               \\-> schedule -> propose -> (chat: narrate) -> END
"""
from functools import partial

from langgraph.graph import END, START, StateGraph

from agent.planning import nodes
from agent.planning.state import PlanningState


def _entry(state):
    t = state.get("trigger")
    if t == "chat":
        return "interpret_request"
    if t in ("cron", "task_event"):
        return "analyze_plan"
    return "load_sources"


def _after_intent(state):
    intent = state.get("intent") or {}
    kind = intent.get("kind")
    if kind == "question":
        return "analyze_plan"
    if kind == "ingest":
        return "load_sources"
    if kind == "decide":
        return "reconcile" if intent.get("decision") == "answer_question" else "apply"
    return "extract_facts"


def _after_analyze(state):
    if state.get("trigger") == "chat":
        return "answer"
    return "reconcile" if state.get("incongruences_in") else END


def _after_validate(state):
    v = state.get("validation") or {}
    if v.get("errors") and (state.get("iteration") or 0) < nodes.MAX_RECONCILE_ROUNDS:
        return "reconcile"
    return "schedule"


def _after_propose(state):
    return "narrate" if state.get("trigger") == "chat" else END


def build_planning_graph(llm):
    g = StateGraph(PlanningState)
    g.add_node("load_project", nodes.load_project)
    g.add_node("load_sources", nodes.load_sources)
    g.add_node("analyze_plan", nodes.analyze)
    g.add_node("validate", nodes.validate_node)
    g.add_node("schedule", nodes.schedule)
    g.add_node("propose", nodes.propose)
    g.add_node("apply", nodes.apply_decision)
    g.add_node("interpret_request", partial(nodes.interpret_request, llm=llm))
    g.add_node("extract_facts", partial(nodes.extract_facts, llm=llm))
    g.add_node("consolidate", partial(nodes.consolidate, llm=llm))
    g.add_node("reconcile", partial(nodes.reconcile, llm=llm))
    g.add_node("link_dependencies", partial(nodes.link_dependencies, llm=llm))
    g.add_node("answer", partial(nodes.answer, llm=llm))
    g.add_node("narrate", partial(nodes.narrate, llm=llm))

    g.add_edge(START, "load_project")
    g.add_conditional_edges("load_project", _entry,
                            ["interpret_request", "analyze_plan", "load_sources"])
    g.add_conditional_edges("interpret_request", _after_intent,
                            ["analyze_plan", "load_sources", "reconcile", "apply", "extract_facts"])
    g.add_edge("load_sources", "extract_facts")
    g.add_edge("extract_facts", "consolidate")
    g.add_edge("consolidate", "reconcile")
    g.add_conditional_edges("analyze_plan", _after_analyze, ["answer", "reconcile", END])
    g.add_edge("answer", END)
    g.add_edge("reconcile", "link_dependencies")
    g.add_edge("link_dependencies", "validate")
    g.add_conditional_edges("validate", _after_validate, ["reconcile", "schedule"])
    g.add_edge("schedule", "propose")
    g.add_conditional_edges("propose", _after_propose, ["narrate", END])
    g.add_edge("narrate", END)
    g.add_edge("apply", END)
    return g.compile()
