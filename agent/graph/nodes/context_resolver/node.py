"""CONTEXT RESOLVER: rewrites ambiguous follow-ups as self-contained messages,
and decides whether they still serve the pending goal.

Key optimization: with an empty history AND no pending goal it does NOT call
the LLM (zero added latency on the first turns, the most common case). Any
error → original message, goal untouched.
"""
from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.history import format_history
from agent.graph.nodes.context_resolver.prompts import RESOLVER_PROMPT
from agent.graph.nodes.context_resolver.schemas import ResolverOutput
from agent.graph.state import AgentState


def _goal_block(goal: dict) -> str:
    """What the model is told about the unfinished job."""
    if not goal:
        return "PENDING GOAL: (none)"
    lines = [f"PENDING GOAL: {goal.get('text', '')}"]
    if goal.get("done_when"):
        lines.append(f"It is done when: {goal['done_when']}")
    return "\n".join(lines)


def _proposal_block(tools: list) -> str:
    """What the assistant asked permission for on the previous turn.

    A confirmation turn executes no tools, so nothing the runner watches can
    record it. Putting the proposal in front of the resolver is what lets it
    tell "yes" from an answer to some OTHER question asked in the same breath.
    """
    if not tools:
        return "PENDING PROPOSAL: (none)"
    return ("PENDING PROPOSAL: the assistant asked the user to authorise "
            + ", ".join(tools)
            + ". Decide ONLY whether THIS message authorises exactly that.")


def context_resolver_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("CONTEXT RESOLVER")
    print("=" * 60)

    history = state.get("history", [])
    message = state.get("user_message", "")
    goal = state.get("pending_goal") or {}
    awaiting = state.get("awaiting_consent") or []

    if goal:
        print(f"   Pending goal: {goal.get('text', '')[:90]}")

    # A pending goal is itself a reason to think, even on a bare history: the
    # whole failure this exists to prevent is a one-word reply arriving with
    # the real request left behind.
    if awaiting:
        print(f"   Awaiting consent for: {', '.join(awaiting)}")

    # A pending PROPOSAL is also a reason to think: the whole failure this
    # guards against is a one-line reply being read as authorisation.
    if not history and not goal and not awaiting:
        print("   → No history, goal or proposal: skipping (no LLM)")
        return {"resolved_message": None, "status": "planning",
                "consent_granted": False}

    try:
        output = llm.with_structured_output(ResolverOutput).invoke([
            SystemMessage(content=RESOLVER_PROMPT),
            HumanMessage(content=(
                f"{_goal_block(goal)}\n\n"
                f"{_proposal_block(awaiting)}\n\n"
                f"HISTORY:\n{format_history(history)}\n\n"
                f"MESSAGE: {message}"
            )),
        ])
    except Exception as e:
        print(f"   Resolver failed ({e}), using original message")
        # Consent is never granted by default: a resolver that could not run
        # has not judged anything, and silence is not authorisation.
        return {"resolved_message": None, "status": "planning",
                "consent_granted": False}

    if output is None:
        print("   Resolver returned nothing, using original message")
        return {"resolved_message": None, "status": "planning",
                "consent_granted": False}

    granted = bool(awaiting) and output.confirms == "yes"
    if awaiting:
        print(f"   → Consent verdict: {output.confirms}"
              + ("  (AUTHORISED)" if granted else "  (not authorised)"))
    update = {"status": "planning", "consent_granted": granted}
    if output.language:
        update["language"] = output.language.strip().lower()[:5]

    # The model may only DROP a goal, never invent one: goals are opened by the
    # runner from what tools actually reported. Letting the resolver open one
    # would put an unverifiable claim where a checkable fact belongs.
    if goal and output.goal_verdict == "abandon":
        print(f"   → Goal abandoned: {output.goal_reason[:100]}")
        update["pending_goal"] = {}
    elif goal:
        print(f"   → Goal kept: {output.goal_reason[:100]}")

    resolved = (output.resolved_message or "").strip()
    if not resolved or resolved.lower() == message.strip().lower():
        print("   → Message was already clear, no changes")
        update["resolved_message"] = None
        return update

    print(f"   → Rewritten: {resolved[:120]}")
    update["resolved_message"] = resolved
    return update
