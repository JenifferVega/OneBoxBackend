"""run_agent(): compatibility wrapper over the LangGraph graph.

Public contract UNCHANGED from the previous agent:
    run_agent(message, history) -> {"response": str, "tools_used": List[str]}
Callers: api/controllers/chat.py, api/services/whatsapp.py, main.py (lambda).

The compiled graph and the LLMs are built ONCE per warm container
(module-level singleton) — avoids recreating Bedrock clients per request in Lambda.
"""
from typing import Any, Dict, List

from agent.graph.state import AgentState

_GRAPH = None

# Last turn's tool results, per session. Graph state does not survive between
# invocations and the frontend sends only text history, so without this the
# planner has no way to see what actually happened last turn -- which is how
# it ended up inventing counts and a failure cause.
# Bounded: a warm container serving many sessions must not grow forever.
_LAST_RESULTS: Dict[str, Dict] = {}
_LAST_RESULTS_MAX = 200

# The user's unfinished request, per session.
#   {"text", "done_when", "done_tool", "opened_at"}
# Opened and closed HERE, from what the tools reported, because both are
# facts: a tool answering `needs_user_choice` did not do its job, and a tool
# returning `next_step` names the call that would finish it. The resolver may
# only abandon a goal -- it never opens one.
_PENDING_GOAL: Dict[str, Dict] = {}
_GOAL_MAX_TURNS = 6          # a goal nobody returns to stops haunting the chat


def _tool_of_step(plan: list, step_num) -> str:
    for s in (plan or []):
        if s.get("step") == step_num:
            return s.get("tool", "")
    return ""


def _close_goal(goal: dict, plan: list, results: dict) -> bool:
    """Did this turn finish the job? True when the declared tool succeeded."""
    want = (goal or {}).get("done_tool")
    if not want:
        return False
    for step_num, res in (results or {}).items():
        if _tool_of_step(plan, step_num) != want:
            continue
        if not isinstance(res, dict):
            continue
        if res.get("error") or res.get("success") is False:
            continue
        if res.get("needs_user_choice"):
            continue          # it ran, and said again that it could not finish
        return True
    return False


def _open_goal(user_message: str, plan: list, results: dict) -> Dict:
    """A goal is opened by EVIDENCE, not by guessing what the user meant.

    Two signals, both written by the tool itself:
      - `needs_user_choice`: the tool ran and did nothing, pending an answer.
        The unfinished job is that tool's own job.
      - `next_step`: the tool succeeded but says the work is not complete, and
        names the call that completes it.
    """
    for step_num, res in (results or {}).items():
        if not isinstance(res, dict):
            continue
        nxt = res.get("next_step") or {}
        if nxt.get("tool"):
            return {"text": user_message, "done_tool": nxt["tool"],
                    "done_when": nxt.get("why", "")}
        if res.get("needs_user_choice"):
            tool = _tool_of_step(plan, step_num)
            if tool:
                return {"text": user_message, "done_tool": tool,
                        "done_when": res.get("error", "")}
    return {}


def _session_key(session_id: str) -> str:
    """Per session, falling back to the current user so the web chat -- which
    does not always send a session_id -- still gets continuity."""
    if session_id:
        return session_id
    try:
        from agent.tools import _current_uid
        return f"uid:{_current_uid()}"
    except Exception:
        return ""


def clear_last_results(session_id: str = "") -> None:
    key = _session_key(session_id)
    _LAST_RESULTS.pop(key, None)
    _PENDING_GOAL.pop(key, None)


def _graph():
    global _GRAPH
    if _GRAPH is None:
        from agent.graph.builder import build_graph
        from agent.graph.llm_factory import create_node_llms
        _GRAPH = build_graph(create_node_llms())
    return _GRAPH


def run_agent(user_message: str, history: List[dict] = None, debug_mode: bool = False, dry_run_projects: list = None, session_id: str = "") -> Dict[str, Any]:
    """Runs the full agent.

    Args:
        user_message: User message
        history: Conversation history [{"role","content"}]

    Returns:
        dict: {"response": str, "tools_used": List[str]}
    """
    print("\n" + "=" * 70)
    print("🤖 ONEBOX AGENT - START")
    print("=" * 70)
    print(f"Message: {user_message}")

    state: AgentState = {
        "user_message": user_message,
        "history": history or [],
        "plan": [],
        "results": {},
        # Writes performed in THIS turn, so a replan cannot repeat one.
        # Starts empty on every turn ON PURPOSE: asking twice is the user
        # asking twice, and must send twice.
        "executed_writes": {},
        "tools_used": [],
        "validation_feedback": None,
        "iteration": 0,
        "status": "resolving",
        "response": "",
        "direct_response": None,
        "debug_mode": debug_mode,
        "session_id": session_id,
        "previous_results": _LAST_RESULTS.get(_session_key(session_id), {}),
        "pending_goal": _PENDING_GOAL.get(_session_key(session_id), {}),
        "intent_draft": None,
        "debug_info": {},
    }

    try:
        final = _graph().invoke(state, config={"recursion_limit": 25})
    except Exception as e:
        print(f"[run_agent] Error in the graph: {e}")
        import traceback
        traceback.print_exc()
        return {"response": "Sorry, I couldn't process your request.", "tools_used": []}

    print("\n" + "=" * 70)
    print("🤖 ONEBOX AGENT - END")
    print("=" * 70)

    # Remember this turn's results for the next one. Only when there ARE
    # results: a turn answered without tools must not erase what the last real
    # execution produced -- that is precisely what gets asked about next.
    turn_results = final.get("results") or {}
    if turn_results:
        key = _session_key(session_id)
        if key:
            if len(_LAST_RESULTS) >= _LAST_RESULTS_MAX and key not in _LAST_RESULTS:
                _LAST_RESULTS.pop(next(iter(_LAST_RESULTS)), None)
            _LAST_RESULTS[key] = turn_results

    # ── The unfinished job ────────────────────────────────────────────────
    # Order matters: CLOSE before OPEN. A turn that finishes the old goal and
    # immediately reports a new one must not have the new one wiped by the
    # close, nor the old one survive because the open ran first.
    key = _session_key(session_id)
    if key:
        # The resolver may have dropped it: it returns {} when the user moved
        # on, and that decision outranks whatever was stored.
        goal = final.get("pending_goal")
        goal = _PENDING_GOAL.get(key, {}) if goal is None else goal
        plan = final.get("plan") or []

        if goal and _close_goal(goal, plan, turn_results):
            print(f"[goal] done: {goal.get('text','')[:80]}")
            goal = {}

        opened = _open_goal(user_message, plan, turn_results)
        if opened:
            # Keep the ORIGINAL wording of an older goal still open: it is the
            # request the user actually made, and the later turns are steps
            # towards it, not new requests.
            if goal and goal.get("text"):
                opened["text"] = goal["text"]
                opened["opened_at"] = goal.get("opened_at", 0)
            goal = opened

        if goal:
            goal["opened_at"] = goal.get("opened_at", 0) + 1
            if goal["opened_at"] > _GOAL_MAX_TURNS:
                print(f"[goal] expired after {_GOAL_MAX_TURNS} turns: "
                      f"{goal.get('text','')[:60]}")
                goal = {}
            else:
                print(f"[goal] pending ({goal['opened_at']}/{_GOAL_MAX_TURNS}): "
                      f"{goal.get('text','')[:70]} -> {goal.get('done_tool','')}")

        if goal:
            if len(_PENDING_GOAL) >= _LAST_RESULTS_MAX and key not in _PENDING_GOAL:
                _PENDING_GOAL.pop(next(iter(_PENDING_GOAL)), None)
            _PENDING_GOAL[key] = goal
        else:
            _PENDING_GOAL.pop(key, None)

    return {
        "response": final.get("response") or "Sorry, I couldn't process your request.",
        "tools_used": final.get("tools_used", []),
        "debug_info": final.get("debug_info"),
        "results": final.get("results", {}),  # raw results per step (for internal callers)
    }
