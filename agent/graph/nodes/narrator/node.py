"""NARRATOR: composes the final response based on the intent.

Intent dispatcher (from the reference pattern):
  - "conversation" → passthrough of direct_response, NO LLM.
  - emails / projects / notifications / proactive / generic → LLM with the
    intent-specific guidance + shared personality.
"""
from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.nodes.narrator.helpers import (NOTHING_CAME_BACK_GUIDANCE,
                                                build_earlier_block,
                                                build_pending_block,
                                                build_user_prompt,
                                                build_verdict_block,
                                                last_resort, truncate_results)
from agent.graph.nodes.narrator.narrators import GUIDANCE_BY_INTENT
from agent.graph.nodes.narrator.prompts import NARRATOR_SYSTEM
from agent.graph.state import AgentState


def narrator_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("NARRATOR")
    print("=" * 60)

    direct_response = state.get("direct_response") or ""
    if direct_response:
        print("   → Using the planner's direct response (no LLM)")
        update = {"response": direct_response, "status": "done"}
        if state.get("debug_mode"):
            update["debug_info"] = {
                **(state.get("debug_info") or {}),
                "narrator": "direct_passthrough",
            }
        return update

    results = state.get("results", {})
    intent = state.get("intent") or "generic"
    # An empty result set is still something to say, and saying it well means
    # saying it in the user's language -- so it goes through the LLM like
    # everything else instead of through a hardcoded English sentence.
    guidance = (NOTHING_CAME_BACK_GUIDANCE if not results
                else GUIDANCE_BY_INTENT.get(intent, GUIDANCE_BY_INTENT["generic"]))
    print(f"   Intent: {intent}" + ("  (no results)" if not results else ""))

    user_message = state.get("resolved_message") or state.get("user_message", "")
    verdict = build_verdict_block(state.get("validation_decision") or "",
                                  state.get("validation_feedback") or "")
    prompt = build_user_prompt(
        user_message,
        truncate_results(results, tools_by_step=state.get("tools_by_step")),
        guidance,
        verdict,
        build_pending_block(state.get("pending_goal") or {}),
        build_earlier_block(results, state.get("tools_by_step") or {},
                            state.get("results_by_tool") or {}),
    )

    try:
        result = llm.invoke([
            SystemMessage(content=NARRATOR_SYSTEM),
            HumanMessage(content=prompt),
        ])
        response = result.content if hasattr(result, "content") else str(result)
    except Exception as e:
        print(f"   Narrator failed: {e}")
        response = last_resort("narrator_failed", state.get("language", ""))

    print(f"   Response: {str(response)[:150]}...")
    update = {"response": response, "status": "done"}
    if state.get("debug_mode"):
        update["debug_info"] = {
            **(state.get("debug_info") or {}),
            "narrator": "llm",
            "intent": intent,
            "results_count": len(results),
            "dry_run": True,
        }
    return update
