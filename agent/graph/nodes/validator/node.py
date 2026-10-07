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


def _called_with(params_by_step, step_num):
    """The parameters a step actually ran with, after from_step resolution.

    The plan shows what the planner WROTE, where a project_id can still be
    {"from_step": 1, "extract": ...} and a filter can be absent. Only the
    resolved params say which project was queried and whether a request for
    pending tasks went out with a status filter at all.
    """
    if not params_by_step:
        return None
    for key in (step_num, str(step_num)):
        if key in params_by_step:
            value = params_by_step[key]
            if isinstance(value, dict):
                return {k: _clip(v) for k, v in value.items()}
            return _clip(value)
    return None


def _summarize_results(results: dict, params_by_step: dict = None) -> str:
    """Render step results for the validator as an index, not a data dump."""
    if not results:
        return "{}"

    lines = []
    for step_num in sorted(results, key=lambda k: (isinstance(k, str), k)):
        r = results[step_num]
        called = _called_with(params_by_step, step_num)

        if not isinstance(r, dict):
            body = {"_called_with": called, "result": _clip(r)} if called is not None else {"result": _clip(r)}
            lines.append(f'"step {step_num}": {json.dumps(body, ensure_ascii=False, default=str)}')
            continue

        # Failures go through verbatim: they are short and they are the point.
        if r.get("_validation_error") or r.get("error") or r.get("success") is False:
            body = dict(r)
            if called is not None:
                body = {"_called_with": called, **body}
            lines.append(f'"step {step_num}": {json.dumps(body, ensure_ascii=False, default=str)[:900]}')
            continue

        digest = {}
        if called is not None:
            digest["_called_with"] = called
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
            return _narrate_with_best(state, results, "ERROR", feedback)
        return {"status": "replan", "validation_feedback": feedback,
                "validation_decision": "ERROR", **_keep_good(state, results)}

    # ── A step was blocked for consent: only the user can unblock it ──
    # Replanning cannot produce authorisation, so sending this round again
    # just burns the iteration budget proposing the same blocked tool. Go
    # straight to the narrator, which turns the block into the question.
    blocked = [r.get("_consent_missing") for r in results.values()
               if isinstance(r, dict) and r.get("_consent_missing")]
    if blocked:
        held = [state.get("tools_by_step", {}).get(k, f"step {k}")
                for k, r in results.items()
                if isinstance(r, dict) and r.get("_held_for_consent")]
        print(f"   Consent missing for {', '.join(blocked)} -> narrate, no replan")
        return {"status": "narrate", "validation_decision": "CONTINUE",
                "validation_feedback": (
                    "The user has not authorised " + ", ".join(blocked) + ". "
                    "Nothing was done with it. Ask for explicit confirmation "
                    "and say exactly what it would do, using `would_do` and "
                    "`would_notify` from its result."
                    + (" These later steps are waiting for the same "
                       "confirmation and were not run either: "
                       + ", ".join(held) + "." if held else ""))}

    # ── Work the tool has already named for a LATER turn ───────────
    # A tool that ran, did part of the job and reported `next_step` pointing
    # at ITSELF is describing a retry, not a planning mistake. The write
    # ledger refuses to run it again in this turn, so a replan can only
    # produce the same result -- observed 2026-09-22: three iterations, six
    # LLM calls, the ledger rejecting each one, and the same partial push.
    # The runner records it as pending; the next turn is where it belongs.
    planned = {s.get("tool") for s in (plan or [])}
    deferred = [r.get("next_step", {}).get("tool") for r in results.values()
                if isinstance(r, dict) and (r.get("next_step") or {}).get("tool")
                and r["next_step"]["tool"] in planned]
    if deferred:
        print(f"   Retry already named for a later turn ({', '.join(deferred)})"
              f" -> narrate, no replan")
        return {"status": "narrate", "validation_decision": "CONTINUE",
                "validation_feedback": (
                    "Part of the work did not complete and is recorded as "
                    "pending: " + ", ".join(deferred) + ". Say plainly what "
                    "was done and what was not. Do not claim it is finished.")}

    # ── Every step failed: there is nothing to judge (no LLM) ──────
    # Deliberately narrow. A PARTIAL failure still goes to the model, because
    # whether a half-done plan satisfies the request is a judgement. And a
    # step carrying needs_user_choice is not a failure at all -- it is a
    # question for the user -- so a result like push_tasks_to_trello's
    # "no list given, offer BOTH options" must never be replanned around.
    steps = list(results.values())
    if steps and all(_failed(r) for r in steps) and not any(
            isinstance(r, dict) and r.get("needs_user_choice") for r in steps):
        feedback = "TOOL FAILURES reported by the system:\n" + "\n".join(
            f"  - step {k}: "
            f"{(r.get('error') or r.get('hint') or 'failed') if isinstance(r, dict) else 'failed'}"
            for k, r in results.items())
        print(f"   Every step failed (no LLM): {feedback[:160]}")
        if iteration >= MAX_PLANNER_ITERATIONS:
            return _narrate_with_best(state, results, "ERROR", feedback)
        return {"status": "replan", "validation_feedback": feedback,
                "validation_decision": "ERROR", **_keep_good(state, results)}

    system = VALIDATOR_PROMPT.format(
        user_message=user_message,
        plan=json.dumps(plan, ensure_ascii=False, indent=2),
        results=_summarize_results(results, state.get("params_by_step")),
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
        return {"status": "narrate", "validation_decision": "DONE",
                "validation_feedback": None}

    print(f"   → {output.decision}: {output.feedback[:150]}")

    if iteration >= MAX_PLANNER_ITERATIONS:
        print("   Limit reached, continuing to the narrator")
        return _narrate_with_best(state, results, output.decision,
                                  output.feedback or output.summary)

    return {"status": "replan",
            "validation_feedback": output.feedback or output.summary,
            "validation_decision": output.decision,
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


def _narrate_with_best(state: AgentState, results: dict,
                       decision: str = "", feedback: str = "") -> dict:
    """Narrate the best attempt, not merely the most recent one.

    Only swaps in the stash when THIS attempt failed and an earlier one did
    not, so a successful retry always wins and nothing is ever hidden from
    the narrator except failures we have something better than.
    """
    verdict = {"validation_decision": decision or "CONTINUE",
               "validation_feedback": feedback}
    if _all_clean(results):
        return {"status": "narrate", **verdict}
    stash = state.get("last_good_results") or {}
    if _all_clean(stash):
        print("   Retries failed -> narrating the last error-free results instead")
        return {"status": "narrate", "results": stash, **verdict}
    return {"status": "narrate", **verdict}
