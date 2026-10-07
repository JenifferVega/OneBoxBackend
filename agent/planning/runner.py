"""run_planning(): the single entry point of the planning graph.

Pure with respect to storage: it receives a project export and returns a
proposal (and, for an approval, the resulting export). Persisting proposals
and applying them to DynamoDB is the caller's job -- which is what lets the
same function run a test case from a folder of JSON files.

LLM: PLANNING_LLM=provider[:model] (same format as NODE_LLM_*). Default: the
planner's routing.
"""
import os
from datetime import date
from typing import Callable, List, Optional

_GRAPH = None


def default_llm():
    from agent.graph.llm_factory import create_llm
    return create_llm(node="planner", env_value=os.environ.get("PLANNING_LLM", ""),
                      temperature=0.1, max_tokens=16000,
                      timeout=int(os.environ.get("PLANNING_LLM_TIMEOUT", "300")),
                      # One provider only, by default: a fallback that fails (Bedrock
                      # legacy model, retired Gemini model) only adds minutes.
                      with_fallbacks=os.environ.get("PLANNING_LLM_FALLBACKS", "off").lower() in ("on", "1", "true"))


def get_graph(llm=None):
    global _GRAPH
    from agent.planning.builder import build_planning_graph
    if llm is not None:
        return build_planning_graph(llm)
    if _GRAPH is None:
        _GRAPH = build_planning_graph(default_llm())
    return _GRAPH


def run_planning(export: dict, trigger: str, *, today: str = "", message: str = "",
                 history: Optional[List[dict]] = None, document: str = "", mode: str = "",
                 open_proposal: Optional[dict] = None, llm=None, documents: Optional[List[str]] = None,
                 on_step: Optional[Callable[[str], None]] = None) -> dict:
    """documents: read only these sources (by name); None = the trigger's default.
    on_step(node): called after each node, for progress in the planning chat."""
    state = {
        "trigger": trigger,
        "today": today or date.today().isoformat(),
        "export": export,
        "message": message,
        "history": history or [],
        "document": document,
        "mode": mode,
        "open_proposal": open_proposal,
        "trace": [],
    }
    if documents is not None:
        state["documents"] = list(documents)
    if message and trigger != "chat":
        # The leader's words travel with the material: "replanea todo con esto".
        state["instruction"] = message
    final: dict = {}
    for snapshot in get_graph(llm).stream(state, config={"recursion_limit": 40}, stream_mode="values"):
        final = snapshot
        if on_step and final.get("trace"):
            try:
                on_step(final["trace"][-1].get("node", ""))
            except Exception:
                pass
    return {
        "proposal": final.get("proposal"),
        "answer": final.get("answer", ""),
        "narration": final.get("narration", ""),
        "result_export": final.get("result_export"),
        "intent": final.get("intent"),
        "facts": final.get("facts", []),
        "topics": final.get("topics", []),
        "analysis": final.get("analysis", {}),
        "trace": final.get("trace", []),
    }
