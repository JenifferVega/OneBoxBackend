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
    #   {"text", "done_when", "done_tool", "opened_at"}
    # Without this, a confirmation like "yes" reached the planner as "create a
    # board" and the request that motivated it ("send the tasks to Trello")
    # existed nowhere in the state: the board was created empty and the turn
    # was declared complete.
    pending_goal: dict

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
    last_good_results: Dict[int, dict]   # snapshot of the last error-free results,
                                         # so a failing replan cannot destroy an
                                         # answer the system already had

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
