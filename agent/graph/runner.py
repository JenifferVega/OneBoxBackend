"""run_agent(): compatibility wrapper over the LangGraph graph.

Public contract UNCHANGED from the previous agent:
    run_agent(message, history) -> {"response": str, "tools_used": List[str]}
Callers: api/controllers/chat.py, api/services/whatsapp.py, main.py (lambda).

The compiled graph and the LLMs are built ONCE per warm container
(module-level singleton) — avoids recreating Bedrock clients per request in Lambda.
"""
from typing import Any, Dict, List

from agent.graph.nodes.narrator.helpers import last_resort
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
#   {"text", "opened_at", "queue": [{"tool", "after", "why"}]}
# Opened and closed HERE, from what the tools reported, because both are
# facts: a tool answering `needs_user_choice` did not do its job, and a tool
# returning `next_step` names the call that would finish it. The resolver may
# only abandon a goal -- it never opens one.
_PENDING_GOAL: Dict[str, Dict] = {}

# What the assistant asked permission for, per session. A confirmation turn
# executes no tools, so _PENDING_GOAL -- which is opened from what tools
# report -- structurally cannot hold it. A proposal survives exactly ONE turn:
# re-asking costs a turn, acting without consent costs a board in the user's
# real Trello account.
_AWAITING_CONSENT: Dict[str, list] = {}
_GOAL_MAX_TURNS = 6          # a goal nobody returns to stops haunting the chat


def _tool_of_step(plan: list, step_num) -> str:
    for s in (plan or []):
        if s.get("step") == step_num:
            return s.get("tool", "")
    return ""


def _succeeded_tools(plan: list, results: dict) -> set:
    """Tools that actually did their job this turn."""
    ok = set()
    for step_num, res in (results or {}).items():
        tool = _tool_of_step(plan, step_num)
        if not tool or not isinstance(res, dict):
            continue
        if res.get("error") or res.get("success") is False:
            continue
        if res.get("needs_user_choice"):
            continue          # it ran, and said again that it could not finish
        ok.add(tool)
    return ok


def _close_goal(goal: dict, plan: list, results: dict) -> dict:
    """Drop the queue entries whose tool succeeded; return what is still left.

    An empty result means the whole job is done. A job is NOT finished because
    ONE of its calls ran: creating the board and never exporting the tasks is
    the case this exists to stop.
    """
    queue = list((goal or {}).get("queue") or [])
    if not queue:
        return {}
    done = _succeeded_tools(plan, results)
    remaining = [e for e in queue if e.get("tool") not in done]
    if not remaining:
        return {}
    if len(remaining) < len(queue):
        print(f"[goal] {len(queue) - len(remaining)} done, "
              f"{len(remaining)} still pending")
    # A dependency that is satisfied stops being one.
    for e in remaining:
        e["after"] = [t for t in (e.get("after") or []) if t not in done]
    return {**goal, "queue": remaining}


def _open_goal(user_message: str, plan: list, results: dict) -> Dict:
    """A goal is opened by EVIDENCE, not by guessing what the user meant.

    Two signals, both written by the tool itself:
      - `needs_user_choice`: the tool ran and did nothing, pending an answer.
        The unfinished job is that tool's own job.
      - `next_step`: the tool succeeded but says the work is not complete, and
        names the call that completes it.
    """
    queue, seen = [], set()
    for step_num, res in (results or {}).items():
        if not isinstance(res, dict):
            continue
        nxt = res.get("next_step") or {}
        if nxt.get("tool") and nxt["tool"] not in seen:
            seen.add(nxt["tool"])
            queue.append({"tool": nxt["tool"],
                          "after": list(nxt.get("after") or []),
                          "why": nxt.get("why", "")})
        elif res.get("needs_user_choice"):
            tool = _tool_of_step(plan, step_num)
            if tool and tool not in seen:
                seen.add(tool)
                queue.append({"tool": tool, "after": [],
                              "why": res.get("error", "")})
    return {"text": user_message, "queue": queue} if queue else {}


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
        "awaiting_consent": _AWAITING_CONSENT.get(_session_key(session_id), []),
        "proposing": [],
        "consent_granted": False,
        "language": "",
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

    # ── What the user was asked to authorise ──────────────────────────────
    # Stored only when THIS turn asked for it; cleared otherwise. A proposal
    # that went unanswered is not a proposal any more.
    _k = _session_key(session_id)
    if _k:
        _proposed = final.get("proposing") or []
        if _proposed:
            _AWAITING_CONSENT[_k] = list(_proposed)
            print(f"[consent] awaiting authorisation for: {', '.join(_proposed)}")
        else:
            _AWAITING_CONSENT.pop(_k, None)

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

        before = (goal or {}).get("text", "")
        if goal:
            goal = _close_goal(goal, plan, turn_results)
            if not goal:
                print(f"[goal] done: {before[:80]}")

        opened = _open_goal(user_message, plan, turn_results)
        if opened:
            # Keep the ORIGINAL wording of an older goal still open: it is the
            # request the user actually made, and the later turns are steps
            # towards it, not new requests. New calls are MERGED into the same
            # queue rather than replacing it -- a turn that discovers more work
            # is adding to the job, not starting a different one.
            if goal and goal.get("queue"):
                have = {e["tool"] for e in goal["queue"]}
                goal["queue"] += [e for e in opened["queue"]
                                  if e["tool"] not in have]
            else:
                # `before` is the wording of the goal this very turn just
                # closed. Without it the new goal is labelled with whatever
                # the user happened to type -- and when that is "si, confirmo",
                # the resolver is shown a pending job that says nothing, and
                # abandons it on the next turn. Observed 2026-09-22: the card
                # lost to a 429 was dropped because its goal was called
                # "si, confirmo".
                keep = (goal or {}).get("text") or before
                if keep:
                    opened["text"] = keep
                    opened["opened_at"] = (goal or {}).get("opened_at", 0)
                goal = opened

        if goal:
            goal["opened_at"] = goal.get("opened_at", 0) + 1
            if goal["opened_at"] > _GOAL_MAX_TURNS:
                print(f"[goal] expired after {_GOAL_MAX_TURNS} turns: "
                      f"{goal.get('text','')[:60]}")
                goal = {}
            else:
                q = goal.get("queue") or []
                ready = [e["tool"] for e in q if not e.get("after")]
                waiting = [e["tool"] for e in q if e.get("after")]
                print(f"[goal] pending ({goal['opened_at']}/{_GOAL_MAX_TURNS}): "
                      f"{goal.get('text','')[:60]} | ready: "
                      f"{', '.join(ready) or '-'} | waiting: "
                      f"{', '.join(waiting) or '-'}")

        if goal:
            if len(_PENDING_GOAL) >= _LAST_RESULTS_MAX and key not in _PENDING_GOAL:
                _PENDING_GOAL.pop(next(iter(_PENDING_GOAL)), None)
            _PENDING_GOAL[key] = goal
        else:
            _PENDING_GOAL.pop(key, None)

    return {
        "response": (final.get("response")
                     or last_resort("no_response", final.get("language", ""))),
        "tools_used": final.get("tools_used", []),
        "debug_info": final.get("debug_info"),
        "results": final.get("results", {}),  # raw results per step (for internal callers)
    }
