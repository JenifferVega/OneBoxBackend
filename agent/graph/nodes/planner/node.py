"""PLANNER: classifies the message and produces a tool plan.

Two tiers (the reference architecture pattern):
  Tier 1 — regex fast-paths (greetings/help/thanks) with no LLM.
  Tier 2 — LLM with structured output (PlannerOutput) + retries + sanitization.
"""
from langchain_core.messages import HumanMessage, SystemMessage

from datetime import date

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
        from agent.tools.access import _accessible_project_ids, _current_uid
        from agent.tools.db import dynamodb, projects_table
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
        wanted = ids[:_MAX_PROJECT_LINES]

        # ONE round trip instead of one per project. This runs on EVERY planner
        # call, so a turn that replans three times used to cost 3 x N reads.
        # batch_get_item returns at most 100 keys and in no particular order,
        # so the items are mapped back by id and rendered in the sorted order.
        by_id = {}
        try:
            keys = [{"projectId": pid} for pid in wanted]
            resp = dynamodb.batch_get_item(
                RequestItems={projects_table.table_name: {"Keys": keys}})
            for item in resp.get("Responses", {}).get(projects_table.table_name, []):
                by_id[item.get("projectId")] = item
            # Whatever the batch could not serve, fetch individually rather
            # than silently rendering those projects as unnamed.
            unprocessed = (resp.get("UnprocessedKeys", {})
                           .get(projects_table.table_name, {}).get("Keys", []))
            for k in unprocessed:
                got = projects_table.get_item(Key=k).get("Item")
                if got:
                    by_id[got.get("projectId")] = got
        except Exception as e:
            print(f"   [planner] batch read failed ({e}), falling back per project")
            for pid in wanted:
                by_id[pid] = projects_table.get_item(
                    Key={"projectId": pid}).get("Item") or {}

        lines = []
        # Whether Trello is connected at all. Without it the planner cannot
        # know, so it plans a Trello call, the tool answers "not connected",
        # and the whole turn is spent on a replan to say what this line says.
        try:
            from api.services.trello import get_status
            st = get_status(_current_uid()) or {}
            if st.get("connected"):
                lines.append("- Trello: connected"
                             + (f' as @{st.get("username")}' if st.get("username") else ""))
            else:
                lines.append("- Trello: NOT connected. No board can be listed, "
                             "created or pushed to until the user connects it.")
        except Exception:
            pass

        for pid in wanted:
            item = by_id.get(pid) or {}
            name = item.get("name", "") or "(unnamed)"
            # The id travels WITH the name. Without it the planner knew the
            # project existed and still had to spend a whole replan running
            # list_projects just to fetch the id it needed to act on.
            if item.get("trelloListId"):
                lines.append(
                    f'- {pid} "{name}" → ALREADY has the Trello board '
                    f'"{item.get("trelloBoardName", "")}" '
                    f'(list "{item.get("trelloListName", "")}")')
            else:
                lines.append(f'- {pid} "{name}" → no Trello board yet')
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
    queue = goal.get("queue") or []
    out = [f'The user asked: "{goal["text"]}"']
    if not queue:
        return "\n".join(out)

    # Split by what is actually runnable now. A call whose dependency has not
    # succeeded yet is not "next", it is blocked, and planning it produces a
    # step that cannot work.
    ready = [e for e in queue if not e.get("after")]
    waiting = [e for e in queue if e.get("after")]
    if ready:
        out.append("STILL TO DO, and runnable NOW:")
        for e in ready:
            out.append(f"  - `{e['tool']}`"
                       + (f" — {e['why']}" if e.get("why") else ""))
    if waiting:
        out.append("STILL TO DO, but WAITING on something first:")
        for e in waiting:
            out.append(f"  - `{e['tool']}` (needs "
                       + ", ".join(f"`{t}`" for t in e["after"])
                       + " to succeed first)"
                       + (f" — {e['why']}" if e.get("why") else ""))
    out.append(
        "Plan the runnable ones in THIS turn, and the waiting ones after the "
        "steps they depend on, in the same plan, chaining with from_step. "
        "Finishing only the first leaves the job half done and makes the user "
        "ask twice for something they already asked for once.\n"
        "This does NOT override MANDATORY CONFIRMATION. A pending call that "
        "writes outside OneBox and has not been authorised yet is still "
        "proposed and confirmed first -- being on this list means it is not "
        "forgotten, never that it is approved.")
    return "\n".join(out)


def _derive_intent(plan: list) -> str:
    """Derive the intent from the plan: a single category → that one; mix → generic."""
    intents = {_TOOL_INTENTS.get(s["tool"], "generic") for s in plan}
    return intents.pop() if len(intents) == 1 else "generic"


def _is_empty_output(o) -> bool:
    """True when the planner's structured output decides nothing.

    No plan AND no reply. Schema-valid (every field of PlannerOutput has a
    default) but not an answer, so it is treated as a failed attempt.
    """
    return not getattr(o, "plan", None) and not (
        getattr(o, "direct_response", None) or "").strip()


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
        # No canned sentence here. This used to answer a Spanish user in
        # English, at the exact moment something had already gone wrong.
        # direct_response=None keeps the narrator from passing anything
        # through, so it writes the reply itself -- with the conversation in
        # front of it, and therefore in the user's language.
        return {
            "status": "direct",
            "intent": state.get("intent") or "generic",
            "direct_response": None,
            "validation_decision": "CONTINUE",
            "validation_feedback": (
                "The planner reached its iteration limit without completing "
                "the request. Say plainly what WAS done and what was not, and "
                "offer to rephrase."
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
        .replace("{today}", date.today().isoformat())
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

    # An EMPTY output counts as a failed attempt, not as an answer.
    #
    # Every field of PlannerOutput has a default, so `{}` is a complete,
    # schema-valid decode: no plan, no reply, no error. Anthropic never
    # returns it; Gemini does, and on 2026-09-22 it did so twice in a row for
    # an ordinary Spanish request that had worked minutes earlier. The loop
    # used to break on `output is not None`, so that empty object was accepted
    # as the planner's decision and the turn ended with nothing.
    #
    # Retrying is what an exception would already have done. The only reason
    # it did not happen is that this particular failure arrives looking like a
    # success.
    output = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            output = structured.invoke([
                SystemMessage(content=system),
                HumanMessage(content=f"User message: {message}"),
            ])
            if output is not None and not _is_empty_output(output):
                break
            if output is not None:
                print(f"   Attempt {attempt}/{LLM_MAX_RETRIES}: the model returned "
                      f"an EMPTY plan AND an empty reply "
                      f"(reasoning={repr(getattr(output, 'reasoning', None))[:120]}). "
                      f"Retrying.")
                output = None
        except Exception as e:
            print(f"   Attempt {attempt}/{LLM_MAX_RETRIES} failed: {e}")

    if output is None:
        # Same rule as the empty-output branch below: no canned English
        # sentence. The narrator answers in the user's language and reports
        # any step that already ran in this turn.
        print("   Planner produced no valid output after retries")
        salvaged = state.get("results") or state.get("last_good_results") or {}
        return {
            "status": "direct",
            "intent": state.get("intent") or "conversation",
            "direct_response": None,
            "results": salvaged,
            "validation_decision": "ERROR",
            "validation_feedback": (
                "The planner could not produce a plan for this message after "
                + str(LLM_MAX_RETRIES) + " attempts (the model returned nothing "
                "usable). "
                + (f"{len(salvaged)} step(s) of an earlier attempt in this same "
                   f"turn DID run and their results are above: report exactly "
                   f"what was done before saying the rest could not be planned."
                   if salvaged else
                   "Nothing was executed. Apologise briefly for the technical "
                   "problem and ask the user to try again.")
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
            "proposing": [],
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
            # Which tools this reply is asking authorisation for. Declared,
            # not inferred from the prose next turn.
            "proposing": list(output.proposing or []),
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

    # The planner returned an empty structured output: no plan, no reply.
    #
    # Every field of PlannerOutput has a default, so `{}` decodes cleanly --
    # and a constrained decoder (Gemini) emits it readily. This branch used to
    # answer with a hardcoded English sentence and status "direct", which the
    # narrator passes through verbatim. Two consequences, both observed live
    # on 2026-09-22:
    #
    #   * A user writing Spanish was answered in English.
    #   * On a replan, the steps of the PREVIOUS iteration had already run --
    #     create_task, update_task, push_tasks_to_trello, send_notification --
    #     and the passthrough discarded their results, so the user was told
    #     "I didn't quite understand your request" AFTER four writes had been
    #     performed on their behalf. In production those writes are real.
    #
    # So: no canned sentence. The turn goes to the narrator with whatever
    # actually ran and a verdict saying planning failed. The narrator answers
    # in the user's language, and reports the work instead of hiding it.
    print("   Output has neither a plan nor a direct response")
    salvaged = state.get("results") or state.get("last_good_results") or {}
    if salvaged:
        print(f"   Narrating the {len(salvaged)} step(s) that DID run "
              f"instead of discarding them")
    return {
        "status": "direct",
        "intent": state.get("intent") or "conversation",
        "direct_response": None,
        "results": salvaged,
        "validation_decision": "ERROR",
        "validation_feedback": (
            "The planner produced no plan and no reply for this message. "
            + (f"{len(salvaged)} step(s) of an earlier attempt in this same turn "
               f"DID run and their results are above: report exactly what was "
               f"done, and then that the rest could not be planned."
               if salvaged else
               "Nothing was executed. Say plainly that the request could not be "
               "understood and ask for it in other words.")
        ),
        "iteration": iteration + 1,
    }
