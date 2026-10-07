"""State of the planning graph (one TypedDict, LangGraph style)."""
from typing import Dict, List, Optional, TypedDict


class PlanningState(TypedDict, total=False):
    # ── input ──
    trigger: str            # document | reanalyze | task_event | cron | chat
    today: str              # YYYY-MM-DD (injected so cases are reproducible)
    export: dict            # project export (GET /api/projects/{id}/export shape)
    message: str            # planning-chat message, or note for task_event/cron
    history: List[dict]     # planning-chat history [{"role","content"}]
    document: str           # file name to read (document / ingest)
    documents: List[str]    # several sources to read (planning chat context)
    mode: str               # "delta" (only what is new) | "full"
    open_proposal: Optional[dict]   # pending proposal (chat 'decide' turns)

    # ── load_project ──
    snapshot: dict
    aliases: Dict[str, str]

    # ── interpret_request ──
    intent: dict

    # ── load_sources / extract_facts ──
    material: List[dict]    # [{"name","text","meta"}]
    facts: List[dict]
    topics: List[dict]      # consolidate: facts grouped, with their state at the end
    people: List[str]

    # ── reconcile / link / validate ──
    instruction: str
    incongruences_in: List[dict]
    operations: List[dict]
    questions: List[dict]
    notes: List[dict]       # things that happened (email, meeting...) -> insights type 'note'
    coverage: List[dict]    # cover call: one entry per fact
    summary: str
    validation: dict
    feedback: str
    iteration: int

    # ── schedule / analyze ──
    analysis: dict
    shifts: List[dict]
    draft_tasks: List[dict]

    # ── output ──
    proposal: Optional[dict]
    answer: str
    narration: str
    result_export: Optional[dict]   # after an approval
    trace: List[dict]
