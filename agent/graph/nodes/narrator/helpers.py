"""Narrator utilities: result truncation and human-prompt assembly."""
import json

MAX_RESULTS_CHARS = 4000


def _label(step, tools_by_step):
    """'step_5 (list_trello_boards)' -- the tool name is what tells a reader
    whether the step READ something or CHANGED something. Without it, a
    successful listing and a successful write are the same shape."""
    tool = (tools_by_step or {}).get(step) or (tools_by_step or {}).get(str(step))
    return f"step_{step} ({tool})" if tool else f"step_{step}"


def truncate_results(results: dict, limit: int = MAX_RESULTS_CHARS,
                     tools_by_step: dict | None = None) -> str:
    """Serialize results respecting the limit, truncating PER STEP
    (largest first) so that small results survive intact instead of
    cutting the whole blob at the end."""
    labelled = {_label(k, tools_by_step): v for k, v in results.items()}
    full = json.dumps(labelled, ensure_ascii=False, default=str, indent=2)
    if len(full) <= limit:
        return full

    # Proportional budget per step, trimming the largest ones first
    serialized = {
        _label(k, tools_by_step): json.dumps(v, ensure_ascii=False, default=str, indent=2)
        for k, v in results.items()
    }
    budget = limit - 50 * max(len(serialized), 1)  # margin for keys/notices
    per_step = max(budget // max(len(serialized), 1), 200)

    parts = []
    for k in sorted(serialized, key=lambda x: len(serialized[x])):
        text = serialized[k]
        if len(text) > per_step:
            text = text[:per_step] + "\n... (step truncated)"
        parts.append(f'"{k}": {text}')
    return "{\n" + ",\n".join(parts) + "\n}"


# When the plan produced nothing. NOT a canned sentence: it goes through the
# same LLM as everything else, so it lands in the user's language.
NOTHING_CAME_BACK_GUIDANCE = """No tool returned any data this turn.

Tell the user plainly that you could not get anything back, in ONE short
paragraph. Do not invent a cause. Ask them to rephrase, or offer the closest
thing you could actually do. Never apologise twice."""


# The genuine last resort: the narrator's own LLM failed, so nothing can be
# phrased on demand. Hardcoded, but at least in the user's language -- an
# English sentence reaching a Spanish user exactly when something already
# went wrong is how a bug becomes a bad experience.
_LAST_RESORT = {
    "narrator_failed": {
        "es": ("Ejecuté tu petición pero tuve un problema al redactar el "
               "resumen. Los datos se procesaron; inténtalo de nuevo para "
               "ver el detalle."),
        "pt": ("Executei o seu pedido mas tive um problema ao redigir o "
               "resumo. Os dados foram processados; tente novamente para "
               "ver os detalhes."),
        "en": ("I ran your request but had a problem writing the summary. "
               "The data was processed correctly; try again to see the "
               "details."),
    },
    "no_response": {
        "es": "No pude procesar tu solicitud.",
        "pt": "Não consegui processar o seu pedido.",
        "en": "Sorry, I couldn't process your request.",
    },
}


def last_resort(kind: str, language: str = "") -> str:
    """A fixed sentence in the user's language, for paths no model can reach."""
    table = _LAST_RESORT.get(kind) or {}
    return table.get((language or "en").split("-")[0].lower(), table.get("en", ""))


def build_verdict_block(decision: str, feedback: str) -> str:
    """What the validator concluded, in the narrator's own prompt.

    The validator produces decision + feedback on every turn and both used to
    die at the validator: nothing in this node read them. So after three failed
    iterations the narrator still saw five results that each said success and
    wrote "I connected the project with Trello" for a turn in which no Trello
    tool had run at all.
    """
    if not decision or decision == "DONE":
        return ""
    return f"""## SYSTEM VERDICT ON THIS TURN: {decision}

{feedback or "(no detail)"}

This verdict outranks how the results look. {decision} means the request was
NOT fully carried out, so do not report it as done. Say plainly what WAS done,
and name what is still missing. A step that only READ something (its tool name
is in the label) never counts as having changed anything."""


def build_earlier_block(results: dict, tools_by_step: dict,
                        results_by_tool: dict, limit: int = 1500) -> str:
    """Reads from EARLIER iterations of this same turn.

    The planner clears `results` on every replan, so what a step found in
    iteration 1 is invisible by iteration 3 -- and the narrator only ever
    sees the last one. That is how three contacts became "both sent
    successfully", with nobody named.
    """
    shown = {(tools_by_step or {}).get(k) or (tools_by_step or {}).get(str(k))
             for k in (results or {})}
    earlier = {t: r for t, r in (results_by_tool or {}).items() if t not in shown}
    if not earlier:
        return ""
    body = json.dumps(earlier, ensure_ascii=False, default=str, indent=2)
    if len(body) > limit:
        body = body[:limit] + "\n... (truncated)"
    return ("## ALSO FOUND EARLIER IN THIS SAME TURN\n"
            "These tools ran while working on this request; their results are "
            "just as real as the ones above and you may quote them.\n" + body)


def build_pending_block(goal: dict) -> str:
    """What the user asked for and is still waiting on.

    The planner has had this all along; the narrator -- the only node that
    actually talks to the user -- did not. So the system would remember an
    unfinished job perfectly, carry it across an unrelated turn and finish it
    later, while the user saw an answer that looked like it had moved on. It
    remembered; it just never said so.
    """
    queue = (goal or {}).get("queue") or []
    if not queue or not (goal or {}).get("text"):
        return ""
    lines = [f'## STILL PENDING from: "{goal["text"]}"']
    for e in queue:
        lines.append(f"  - {e.get('tool')}"
                     + (f" -- {e['why']}" if e.get("why") else ""))
    lines.append(
        "Close your reply with ONE short line telling the user this is still "
        "open and that you have not forgotten it, in their own words -- never "
        "the tool names. One line, not a list, and not a re-telling of what "
        "you just said.")
    return "\n".join(lines)


def build_user_prompt(user_message: str, results_text: str, guidance: str,
                      verdict: str = "", pending: str = "",
                      earlier: str = "") -> str:
    """Build the narrator's human message with the intent-specific guidance."""
    verdict_block = ("\n" + verdict + "\n") if verdict else ""
    pending_block = ("\n" + pending + "\n") if pending else ""
    earlier_block = ("\n" + earlier + "\n") if earlier else ""
    return f"""## User message:
{user_message}

## Results obtained (each step labelled with the tool that produced it):
{results_text}

Those tool names are INTERNAL. They are there so you can tell a step that
merely READ something from one that CHANGED something. Never write a tool name
in your reply, and never offer the user a list of them: they are talking to an
assistant, not calling an API. Say "I can show you the project's tasks", not
"you can ask me for list_tasks".
{earlier_block}{verdict_block}{pending_block}
{guidance}

Report what happened: what was done, and what was not. Never describe a step
whose tool only read data as if it had changed something.

Do NOT invent a list of things you could do next. The only follow-up you may
offer is real pending work, and only when a STILL PENDING section appears
above. With no such section, stop once you have answered: a menu of
capabilities nobody asked for is filler, and offering something the system
cannot actually do is worse than filler."""
