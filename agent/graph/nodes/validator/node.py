"""VALIDATOR: checks whether the results satisfy the request.

DONE → narrate. ERROR/CONTINUE with iterations available → replan with
feedback. Limit reached → narrate anyway (graceful degradation, same as the
previous agent). LLM failure → DONE (never blocks the response).
"""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.nodes.validator.prompts import VALIDATOR_PROMPT
from agent.graph.nodes.validator.schemas import ValidatorOutput
from agent.graph.state import MAX_PLANNER_ITERATIONS, AgentState


def _extract_validation_errors(results: dict) -> list[str]:
    """Detect executor validation errors in the results."""
    errors = []
    for step_num, result in results.items():
        if isinstance(result, dict) and result.get("_validation_error"):
            errors.append(result.get("hint", result.get("error", "Unknown validation error")))
    return errors


# What the validator is shown.
#
# It used to receive the raw results JSON cut at 2000 characters, which is how
# a correct plan got rejected: step 1 (list_projects, five full DynamoDB
# records) consumed the budget, step 2's successful list_tasks never reached
# the prompt, and the validator concluded the project "did not exist" from
# data it was never shown.
#
# Raising the cap would only postpone that. Look at what this node is actually
# asked: were tasks created for every phase, do they carry assigned_to, did a
# tool error, is count 0. Every criterion is about the SHAPE of the results.
# None of them needs createdAt, channels, or a project's 200-character
# description -- and those are what filled the budget.
#
# So we project instead of truncate: per step, the tool, whether it worked,
# how many items came back, and the identifying fields of each one. The
# omitted field names are listed too, so "I cannot see it" is never mistaken
# for "it is not there". The result is smaller than the old 2000-char cut and
# strictly more informative.
_MAX_ITEMS = 25          # per list; beyond this we say how many were elided
_MAX_VALUE = 120         # per field
_RESULTS_BUDGET = 8000   # backstop only; the projection lands far below it

# Fields that identify a record. Anything else is bulk the validator cannot use.
_IDENTIFYING = (
    "name", "projectName", "projectId", "taskId", "text", "title", "subject",
    "status", "assignedTo", "assigned_to", "dueDate", "type", "role",
    "email", "phone", "recipient", "channel", "score", "count", "scheduled_at",
)


def _clip(v):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= _MAX_VALUE else s[:_MAX_VALUE] + "..."


def _project(item):
    """Keep only the fields that identify a record; report the rest by name."""
    if not isinstance(item, dict):
        return _clip(item)
    kept = {k: _clip(v) for k, v in item.items() if k in _IDENTIFYING and v not in (None, "", [])}
    omitted = [k for k in item if k not in _IDENTIFYING]
    if omitted:
        kept["_omitted_fields"] = omitted
    return kept


def _summarize_results(results: dict) -> str:
    """Render step results for the validator as an index, not a data dump."""
    if not results:
        return "{}"

    lines = []
    for step_num in sorted(results, key=lambda k: (isinstance(k, str), k)):
        r = results[step_num]
        if not isinstance(r, dict):
            lines.append(f'"step {step_num}": {_clip(r)}')
            continue

        # Failures go through verbatim: they are short and they are the point.
        if r.get("_validation_error") or r.get("error") or r.get("success") is False:
            lines.append(f'"step {step_num}": {json.dumps(r, ensure_ascii=False, default=str)[:600]}')
            continue

        digest = {}
        for k, v in r.items():
            if isinstance(v, list):
                shown = [_project(i) for i in v[:_MAX_ITEMS]]
                digest[k] = {"count": len(v), "items": shown}
                if len(v) > _MAX_ITEMS:
                    digest[k]["_note"] = (
                        f"{len(v) - _MAX_ITEMS} more item(s) exist but are not listed here. "
                        f"They EXIST -- do not treat them as missing."
                    )
            elif k in _IDENTIFYING or not isinstance(v, (dict, list)):
                digest[k] = _clip(v)
            else:
                digest[k] = _project(v)
        lines.append(f'"step {step_num}": {json.dumps(digest, ensure_ascii=False, default=str)}')

    out = "{\n  " + ",\n  ".join(lines) + "\n}"
    if len(out) > _RESULTS_BUDGET:
        out = out[:_RESULTS_BUDGET] + (
            "\n... [TRUNCATED. The data continues -- do NOT treat anything you "
            "cannot see here as missing or non-existent.]"
        )
    return out


def _failed(result) -> bool:
    """True if a step result represents a failure rather than data."""
    return isinstance(result, dict) and (
        result.get("_validation_error") or result.get("error")
        or result.get("success") is False
    )


def _all_clean(results: dict) -> bool:
    """True if every step in this attempt produced data rather than an error.

    Deliberately NOT "does any step look usable". The retry that motivated
    this had a perfectly good step 1 (resolve_entity found the project) and
    two failed steps after it; judging by "is anything usable" would call
    that attempt fine and still hand the narrator the failures.
    """
    return bool(results) and not any(_failed(r) for r in results.values())


def validator_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("VALIDATOR")
    print("=" * 60)

    user_message = state.get("resolved_message") or state.get("user_message", "")
    plan = state.get("plan", [])
    results = state.get("results", {})
    iteration = state.get("iteration", 0)

    # ── Fast detection of validation errors (no LLM) ──────────────
    validation_errors = _extract_validation_errors(results)
    if validation_errors:
        feedback = "PARAMETER ERRORS detected by the system:\n" + "\n".join(
            f"  - {e}" for e in validation_errors
        )
        print(f"   Code validation failed: {feedback[:200]}")
        if iteration >= MAX_PLANNER_ITERATIONS:
            print("   Limit reached, continuing to the narrator anyway")
            return _narrate_with_best(state, results)
        return {"status": "replan", "validation_feedback": feedback,
                **_keep_good(state, results)}

    system = VALIDATOR_PROMPT.format(
        user_message=user_message,
        plan=json.dumps(plan, ensure_ascii=False, indent=2),
        results=_summarize_results(results),
    )

    try:
        output = llm.with_structured_output(ValidatorOutput).invoke([
            SystemMessage(content=system),
            HumanMessage(content="Evaluate the results."),
        ])
    except Exception as e:
        print(f"   Validator failed ({e}), assuming DONE")
        output = None

    if output is None or output.decision == "DONE":
        summary = (output.summary if output else "")[:100]
        print(f"   → Validation successful: {summary}")
        return {"status": "narrate"}

    print(f"   → {output.decision}: {output.feedback[:150]}")

    if iteration >= MAX_PLANNER_ITERATIONS:
        print("   Limit reached, continuing to the narrator")
        return _narrate_with_best(state, results)

    return {"status": "replan",
            "validation_feedback": output.feedback or output.summary,
            **_keep_good(state, results)}


# ── Protecting a good answer from a bad replan ──────────────────────────────
# The executor keys results by step number and carries them across
# iterations, so a replan writes over the previous plan's results at the same
# index. A replan that fails therefore DESTROYS data the system had already
# fetched successfully: the narrator ends up reporting errors for a question
# it could already answer. These two helpers keep the last clean set aside
# and hand it back if the retries never produce anything better.

def _keep_good(state: AgentState, results: dict) -> dict:
    """Stash the current results if they are clean, before a replan overwrites them."""
    if _all_clean(results):
        return {"last_good_results": results}
    return {}


def _narrate_with_best(state: AgentState, results: dict) -> dict:
    """Narrate the best attempt, not merely the most recent one.

    Only swaps in the stash when THIS attempt failed and an earlier one did
    not, so a successful retry always wins and nothing is ever hidden from
    the narrator except failures we have something better than.
    """
    if _all_clean(results):
        return {"status": "narrate"}
    stash = state.get("last_good_results") or {}
    if _all_clean(stash):
        print("   Retries failed -> narrating the last error-free results instead")
        return {"status": "narrate", "results": stash}
    return {"status": "narrate"}
