"""Conversion of the OneBox history ({"role","content"}) to graph formats."""
from typing import List

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


def to_lc_messages(history: List[dict]) -> List[BaseMessage]:
    """Converts the OneBox history to LangChain messages."""
    msgs: List[BaseMessage] = []
    for m in history or []:
        content = m.get("content", "")
        if m.get("role") == "assistant":
            msgs.append(AIMessage(content))
        else:
            msgs.append(HumanMessage(content))
    return msgs


def format_history(history: List[dict], max_messages: int = 10, max_chars: int = 350) -> str:
    """Formats history as plain text for injecting into prompts.

    Last 10 messages, 350 characters per message — preserves IDs, names
    and referenceable data that appear in previous turns.
    """
    if not history:
        return "No previous history."
    lines = []
    for msg in history[-max_messages:]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        content = (msg.get("content", "") or "")[:max_chars]
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)


def format_previous_results(results: dict, budget: int = 2500) -> str:
    """Render the PREVIOUS turn's tool results for the planner prompt.

    Why this exists: a direct_response skips the narrator, so nothing checks
    it. Asked "how many failed?", the planner only had the conversation
    history -- a summary, not data -- and it invented figures and a cause.
    Giving it the real results removes the need to remember: the numbers are
    in front of it.

    Budget is split per step so one big result cannot push the others out,
    and every cut is labelled.
    """
    if not results:
        return "No results from previous turns."

    import json
    per_step = max(250, budget // max(len(results), 1))
    lines = []
    for step in sorted(results, key=lambda k: (isinstance(k, str), k)):
        blob = json.dumps(results[step], ensure_ascii=False, default=str)
        if len(blob) > per_step:
            blob = blob[:per_step] + f"... [cut, {len(blob) - per_step} more chars]"
        lines.append(f"- step {step}: {blob}")
    return "\n".join(lines)
