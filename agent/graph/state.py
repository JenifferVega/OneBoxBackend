"""Typed state of the OneBox agent graph.

A single TypedDict flows through all nodes (LangGraph pattern). Each node
returns ONLY the keys it modifies; LangGraph merges them into the state.
"""
from typing import Dict, List, Optional, TypedDict

# Maximum number of planner replans (preserves the previous agent's behavior).
MAX_PLANNER_ITERATIONS = 3


class AgentState(TypedDict, total=False):
    # ── Input (set by the runner before invoking the graph) ──
    user_message: str            # raw message (may include project context prepended by WhatsApp)
    history: List[dict]          # [{"role": "user"|"assistant", "content": str}]

    # ── context_resolver ──
    resolved_message: Optional[str]   # self-contained rewritten message; None if unchanged

    # Something the user asked for that is NOT done yet, carried across turns.
    # OPENED and CLOSED by the runner from what tools report -- a tool saying
    # `needs_user_choice` means it did not do its job, and a tool returning
    # `next_step` names the call that would finish it. The resolver may only
    # ABANDON it, when the user has moved on.
    #   {"text", "opened_at", "queue": [{"tool", "after", "why"}]}
    # The queue is a small dependency graph, not a list: `after` names the
    # tools that must SUCCEED before that call can run, so the planner can
    # tell what it may do now from what has to wait. The edges come from the
    # tools themselves (`next_step.after`), the same way from_step already
    # expresses order inside a single plan.
    # It remembers; it does not drive. A queued write still goes through the
    # consent gate: nothing reaches a third party because of something the
    # user said three turns ago.
    # Without this, a confirmation like "yes" reached the planner as "create a
    # board" and the request that motivated it ("send the tasks to Trello")
    # existed nowhere in the state: the board was created empty and the turn
    # was declared complete.
    pending_goal: dict

    # Language the user wrote in (ISO 639-1), detected by the resolver.
    # The degraded paths -- narrator LLM down, empty response -- cannot call a
    # model to phrase themselves, and a hardcoded English sentence reaches a
    # Spanish user exactly when something has already gone wrong.
    language: str

    # ── consent ──
    # A turn that ASKS for authorisation runs no tools at all, so pending_goal
    # (which the runner opens from what tools report) can never carry it.
    # Without these, "yes" and "upload the pending ones" look identical to the
    # planner, and an answer to a DIFFERENT question reads as consent -- which
    # is how a Trello board got created in a turn that authorised nothing.
    proposing: List[str]         # tools THIS turn asks permission for
    awaiting_consent: List[str]  # tools the PREVIOUS turn asked about
    consent_granted: bool        # the resolver's verdict on this message

    # ── planner ──
    plan: List[dict]             # [{"step": int, "tool": str, "params": dict}]
    direct_response: Optional[str]    # response without tools (greeting, help, rejection)
    intent: Optional[str]        # emails | projects | notifications | proactive | conversation | generic
    resolution_method: Optional[str]  # "regex" (fast-path) | "llm"

    # ── executor ──
    results: Dict[int, dict]     # per-step results (from_step references depend on this)
    tools_used: List[str]

    # Every WRITE already performed in THIS turn, keyed by tool + resolved
    # parameters. A replan re-runs the plan from step 1, so without this ledger
    # a step that already succeeded is executed again: two Trello boards, two
    # WhatsApps, two invitations -- for ONE user request. `results` could not
    # serve as the ledger because it is keyed by step NUMBER, and a replan is
    # free to put a different tool at the same index.
    executed_writes: Dict[str, dict]   # fingerprint -> {"step", "tool", "result"}

    # ── validator ──
    validation_feedback: Optional[str]   # error context for the replan
    validation_decision: Optional[str]   # DONE | CONTINUE | ERROR, carried to
                                         # the narrator so it cannot report a
                                         # turn as finished that was not
    last_good_results: Dict[int, dict]   # snapshot of the last error-free results,
                                         # so a failing replan cannot destroy an
                                         # answer the system already had

    # Which tool produced each step's result. `results` is keyed by step
    # NUMBER only, so a narrator reading it cannot tell a list_trello_boards
    # (a read) from a link_project_to_trello (a write) -- which is how a
    # successful board LISTING was reported as "I connected the project".
    tools_by_step: Dict[int, str]

    # The last result of each tool run ANYWHERE in this turn, keyed by tool.
    # `results` is keyed by step number and the planner clears it on every
    # replan, so a read from iteration 1 is gone by the time the narrator
    # writes. Observed 2026-09-22: get_project_contacts returned three people,
    # two were messaged, and the reply said "both sent successfully" and named
    # neither -- the narrator no longer had the contacts, only the phone
    # numbers buried in the send parameters.
    results_by_tool: Dict[str, dict]

    # The parameters each step RAN with, after from_step references were
    # resolved. The validator used to see only the plan as the planner wrote
    # it, where a project_id can be {"from_step": 1, ...} -- so it could not
    # tell WHICH project was queried, nor that a request for pending tasks
    # had gone out with no status filter and brought back the finished ones.
    params_by_step: Dict[int, dict]

    # ── narrator ──
    response: str

    # ── Debug / dry-run ──
    debug_mode: bool              # if True, the executor simulates tools without touching DynamoDB
    session_id: Optional[str]     # MCP session ID (for per-session dry-run cache)
    intent_draft: Optional[dict]  # partial intent schema under construction
    debug_info: Optional[dict]    # accumulated debug info returned to the caller

    # ── Flow control ──
    iteration: int               # planner replan counter (capped at MAX_PLANNER_ITERATIONS)
    status: str                  # resolving→planning→executing→validating→narrating→done
                                 # transient: "direct" (planner→narrator), "replan" (validator→planner)
