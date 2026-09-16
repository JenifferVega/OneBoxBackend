"""PLANNER: classifies the message and produces a tool plan.

Two tiers (the reference architecture pattern):
  Tier 1 — regex fast-paths (greetings/help/thanks) with no LLM.
  Tier 2 — LLM with structured output (PlannerOutput) + retries + sanitization.
"""
from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.history import format_history, format_previous_results
from agent.graph.nodes.planner.patterns import match_fast_path
from agent.graph.nodes.planner.prompts import PLANNER_PROMPT
from agent.graph.nodes.planner.schemas import PlannerOutput
from agent.graph.state import MAX_PLANNER_ITERATIONS, AgentState
from agent.tools import TOOL_MAP

LLM_MAX_RETRIES = 3
MAX_PLAN_STEPS = 12

# Tool → intent map (for the narrator dispatcher)
_TOOL_INTENTS = {
    "list_emails": "emails",
    "inspect_email": "emails",
    "send_email": "emails",
    "list_projects": "projects",
    "create_project": "projects",
    "create_task": "projects",
    "list_tasks": "projects",
    "update_task": "projects",
    "delete_task": "projects",
    "update_project": "projects",
    "invite_user": "projects",
    "update_participants": "projects",
    "remove_participant": "projects",
    "assign_email_to_project": "projects",
    "create_insight": "projects",
    "create_reminder": "projects",
    "send_notification": "notifications",
    "list_notifications": "notifications",
    "get_project_contacts": "notifications",
    "resolve_person": "notifications",
    "analyze_inbox": "proactive",
    "check_sla": "proactive",
    "auto_classify_messages": "proactive",
    "proactive_summary": "proactive",
}


_MAX_PROJECT_LINES = 40


def _project_board_lines() -> str:
    """Which of the user's projects already have a Trello board.

    Placed in EVERY planner prompt, because the turn that gets this wrong
    calls no tool at all. Asked "create a Trello board for X", the planner
    answers with a bare proposal -- no plan, no steps -- so nothing put in a
    tool result can reach it, however well that result is written. Three
    attempts to fix this with instructions failed for the same reason: there
    was no lookup to instruct.

    A fact in front of it needs no lookup. Cheap: the same per-project read
    the projects endpoint already does, and bounded.
    """
    try:
        from agent.tools.access import _accessible_project_ids
        from agent.tools.db import projects_table
    except Exception:
        return "(not available)"
    try:
        # _accessible_project_ids returns a SET, which cannot be sliced --
        # 'set' object is not subscriptable sent this whole block to the
        # except branch, so it silently rendered "(not available)" on every
        # turn. Sorted for a stable order between turns.
        ids = sorted(_accessible_project_ids())
        if not ids:
            return "(this user has no projects)"
        lines = []
        for pid in ids[:_MAX_PROJECT_LINES]:
            item = projects_table.get_item(Key={"projectId": pid}).get("Item") or {}
            name = item.get("name", "") or "(unnamed)"
            if item.get("trelloListId"):
                lines.append(
                    f'- "{name}" → ALREADY has the Trello board '
                    f'"{item.get("trelloBoardName", "")}" '
                    f'(list "{item.get("trelloListName", "")}")')
            else:
                lines.append(f'- "{name}" → no Trello board yet')
        if len(ids) > _MAX_PROJECT_LINES:
            lines.append(f"- ... and {len(ids) - _MAX_PROJECT_LINES} more")
        return "\n".join(lines)
    except Exception as e:
        # Never break planning over this: it is context, not a dependency.
        print(f"   [planner] could not list project boards: {e}")
        return "(not available)"


def _goal_text(goal: dict) -> str:
    """Render the unfinished job for the prompt. Empty is stated explicitly:
    a blank slot reads as a missing variable, and models fill blanks."""
    if not goal or not goal.get("text"):
        return "(none — nothing is pending)"
    out = [f'The user asked: "{goal["text"]}"']
    if goal.get("done_tool"):
        out.append(f"It is done when `{goal['done_tool']}` runs successfully.")
    if goal.get("done_when"):
        out.append(f"Why it is still pending: {goal['done_when']}")
    return "\n".join(out)


def _derive_intent(plan: list) -> str:
    """Derive the intent from the plan: a single category → that one; mix → generic."""
    intents = {_TOOL_INTENTS.get(s["tool"], "generic") for s in plan}
    return intents.pop() if len(intents) == 1 else "generic"


def planner_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("PLANNER")
    print("=" * 60)

    message = state.get("resolved_message") or state.get("user_message", "")
    iteration = state.get("iteration", 0)
    feedback = state.get("validation_feedback") or "None"

    print(f"   Message: {message[:100]}...")
    print(f"   Iteration: {iteration + 1}/{MAX_PLANNER_ITERATIONS}")

    if iteration >= MAX_PLANNER_ITERATIONS:
        print("   Iteration limit reached")
        return {
            "status": "direct",
            "intent": "conversation",
            "direct_response": (
                "I couldn't complete your request after several attempts. "
                "Could you rephrase your question?"
            ),
            "iteration": iteration + 1,
        }

    # ── Tier 1: regex fast-paths (only on first attempt, no feedback) ──
    if iteration == 0 and not state.get("validation_feedback"):
        canned = match_fast_path(message)
        if canned:
            print("   Regex fast-path (no LLM)")
            return {
                "status": "direct",
                "intent": "conversation",
                "direct_response": canned,
                "resolution_method": "regex",
                "iteration": iteration + 1,
            }

    # ── Tier 2: LLM with structured output ──
    # .replace() instead of .format(): the catalog contains literal JSON with braces
    system = (
        PLANNER_PROMPT
        .replace("{history}", format_history(state.get("history", [])))
        .replace("{validator_feedback}", feedback)
        .replace("{pending_goal}", _goal_text(state.get("pending_goal")))
        .replace("{previous_results}", format_previous_results(state.get("previous_results", {})))
        # What THIS turn's steps have already returned. On a replan the planner
        # used to see only the validator's prose -- "the plan did not create the
        # board" -- and never the data its own step had just fetched. So it
        # proposed creating a board for a project that resolve_entity had just
        # reported as already linked, one iteration earlier. Facts placed in
        # tool results could not reach the planner at all within a turn.
        .replace("{results_this_turn}", format_previous_results(state.get("results", {})))
        .replace("{project_boards}", _project_board_lines())
    )
    structured = llm.with_structured_output(PlannerOutput)

    output = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            output = structured.invoke([
                SystemMessage(content=system),
                HumanMessage(content=f"User message: {message}"),
            ])
            if output is not None:
                break
        except Exception as e:
            print(f"   Attempt {attempt}/{LLM_MAX_RETRIES} failed: {e}")

    if output is None:
        print("   Planner produced no valid output after retries")
        return {
            "status": "direct",
            "intent": "conversation",
            "direct_response": (
                "Sorry, I had a problem processing your request. "
                "Can you try again?"
            ),
            "iteration": iteration + 1,
        }

    # ── DEBUG: show full LLM output ──
    print(f"   [DEBUG] direct_response: {repr(output.direct_response)}")
    print(f"   [DEBUG] raw plan ({len(output.plan)} steps):")
    for s in output.plan:
        print(f"      step={s.step} tool={s.tool} params={s.params}")

    # ── Sanitization (guard against LLM hallucinations) ──
    plan = []
    for step in output.plan[:MAX_PLAN_STEPS]:
        if step.tool not in TOOL_MAP:
            print(f"   Unknown tool discarded: {step.tool}")
            continue
        plan.append({"step": step.step, "tool": step.tool, "params": step.params or {}})

    if plan:
        print(f"   → Plan generated: {len(plan)} steps")
        for s in plan:
            print(f"      Step {s['step']}: {s['tool']} - {s['params']}")
        update = {
            "status": "executing",
            "plan": plan,
            "intent": _derive_intent(plan),
            "results": {},
            "tools_used": [],
            "resolution_method": "llm",
            "validation_feedback": None,
            "iteration": iteration + 1,
        }
        if state.get("debug_mode"):
            update["debug_info"] = {
                **(state.get("debug_info") or {}),
                "planner_decision": "execute_plan",
                "plan": plan,
                "intent": _derive_intent(plan),
                "iteration": iteration + 1,
                "resolution_method": "llm",
            }
        return update

    if output.direct_response:
        print("   → Direct response (no tools)")
        update = {
            "status": "direct",
            "intent": "conversation",
            "direct_response": output.direct_response,
            "plan": [],
            "resolution_method": "llm",
            "iteration": iteration + 1,
        }
        if state.get("debug_mode"):
            update["debug_info"] = {
                **(state.get("debug_info") or {}),
                "planner_decision": "direct_response",
                "direct_response": output.direct_response,
                "iteration": iteration + 1,
                "resolution_method": "llm",
            }
        return update

    print("   Output has neither a plan nor a direct response")
    return {
        "status": "direct",
        "intent": "conversation",
        "direct_response": "I didn't quite understand your request. Could you be more specific?",
        "iteration": iteration + 1,
    }
