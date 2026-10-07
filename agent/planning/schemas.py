"""Structured outputs of the planning graph.

Every LLM node in agent/planning answers in one of these shapes. Two rules,
both learned the hard way in the chat graph (see README, "The 2026-09-16
incident"):

  * No bare `dict` fields anywhere. Gemini decodes under the schema, and a
    field typed as an open object comes back empty. Everything is a list of
    small, fully specified models.
  * No empty string inside an enum: Gemini rejects the whole schema
    ("enum[0]: cannot be empty"). Optional choices are plain strings that the
    code normalises.
  * Every field has a default, so an empty decode is VALID -- which means the
    caller must treat "valid but empty" as a failed attempt, not as an answer.
"""
from typing import List, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    source: str = Field("", description="File name, 'chat' (planning chat) or 'feedback'")
    locator: str = Field("", description="Where in the source: timestamp like '9/21 17:34', minute '28:01' or section '7'")
    quote: str = Field("", description="Short literal quote (max ~25 words) that supports the claim")


# ── extract_facts ──────────────────────────────────────────────────────────
FactKind = Literal["done", "in_progress", "new_work", "blocked", "unblocked",
                   "date_commitment", "decision", "risk", "person", "request_to_client"]


class Fact(BaseModel):
    kind: FactKind = "new_work"
    text: str = Field("", description="The fact, one sentence, in the source language")
    who: str = Field("", description="Person responsible or who reported it, as named in the source")
    date: str = Field("", description="YYYY-MM-DD when the source states or clearly implies it, else empty")
    evidence: List[Evidence] = Field(default_factory=list)


class FactsOutput(BaseModel):
    facts: List[Fact] = Field(default_factory=list)
    people: List[str] = Field(default_factory=list, description="Every person named in the source")
    last_event_date: str = Field("", description="Date of the most recent event in the source, YYYY-MM-DD")


# ── consolidate ────────────────────────────────────────────────────────────
class Topic(BaseModel):
    topic: str = Field("", description="Short name of the piece of work / problem / decision line")
    state: str = Field("", description="open | in_progress | blocked | done | dropped | info, at the END")
    summary: str = Field("", description="Story and CURRENT state in 1-2 sentences, with dates")
    owner: str = Field("", description="Who must act now, empty if nobody")
    due_date: str = Field("", description="YYYY-MM-DD only when a fact states a deadline")
    date_status: str = Field("", description="'committed' when due_date comes from a fact, else empty")
    last_date: str = Field("", description="Date of the latest fact of the topic")
    facts: List[int] = Field(default_factory=list, description="Numbers N of the facts (FN) in this topic")
    evidence: List[Evidence] = Field(default_factory=list)


class TopicsOutput(BaseModel):
    topics: List[Topic] = Field(default_factory=list)


# ── reconcile / link_dependencies ──────────────────────────────────────────
OpKind = Literal["update", "close", "reopen", "delete", "create", "link", "block"]


class Operation(BaseModel):
    op: OpKind = "update"
    task_id: str = Field("", description="Existing taskId. For 'create' use a new id like 'new-1'")
    text: str = Field("", description="Task title. Required for create; for update only if it changes")
    status: str = Field("", description="pending | in_progress | blocked | done. Empty = unchanged")
    assigned_to: str = Field("", description="Person responsible, as named in the project. Empty = unchanged")
    start_date: str = Field("", description="YYYY-MM-DD. Empty = unchanged / none")
    due_date: str = Field("", description="YYYY-MM-DD. Empty = unchanged / none")
    date_status: str = Field("", description="'estimated', 'committed' or empty")
    depends_on: List[str] = Field(default_factory=list, description="taskIds (or new ids) that must finish first")
    blocked_reason: str = ""
    duplicate_of: str = Field("", description="For delete of a duplicate: the taskId that stays")
    reason: str = Field("", description="Why, one sentence")
    evidence: List[Evidence] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class Question(BaseModel):
    id: str = ""
    text: str = Field("", description="Question for the project leader, in the user's language")
    about_task_ids: List[str] = Field(default_factory=list)
    options: List[str] = Field(default_factory=list)


class TaskReview(BaseModel):
    task_id: str = ""
    latest_fact: int = Field(0, description="N of the MOST RECENT fact about this task (FN), 0 if none")
    now: str = Field("", description="Real state of the task today, a few words")
    verdict: str = Field("", description="keep | update | close | delete | block | reopen | question")


class ReconcileOutput(BaseModel):
    reviews: List[TaskReview] = Field(default_factory=list, description="ONE per task under CURRENT TASKS")
    operations: List[Operation] = Field(default_factory=list)
    questions: List[Question] = Field(default_factory=list)
    summary: str = Field("", description="Two sentences: what changed and why")


# ── cover_facts (the call that creates) ────────────────────────────────────
class Note(BaseModel):
    """Something that HAPPENED (an email, a meeting, a call, a decision) and is
    worth keeping as context, but is not work to do. It is never a task."""
    kind: str = Field("info", description="email | meeting | call | decision | idea | info")
    text: str = Field("", description="What happened, one sentence, in the language of the project")
    who: str = ""
    date: str = Field("", description="YYYY-MM-DD when known")
    task_id: str = Field("", description="Related taskId (existing or 'new-N'), or empty")
    evidence: List[Evidence] = Field(default_factory=list)


class Coverage(BaseModel):
    fact: int = Field(0, description="The fact number N of 'FN'")
    action: str = Field("", description="covered | create | note | ignore")
    task_id: str = Field("", description="covered: the existing taskId; create: the new id; note: related task or empty")


class CoverOutput(BaseModel):
    coverage: List[Coverage] = Field(default_factory=list, description="ONE entry per fact, in order")
    creates: List[Operation] = Field(default_factory=list, description="Only op='create' items")
    notes: List[Note] = Field(default_factory=list)
    questions: List[Question] = Field(default_factory=list)
    summary: str = ""


class LinkOutput(BaseModel):
    links: List[Operation] = Field(default_factory=list, description="Only op='link' items")


# ── interpret_request (planning chat) ──────────────────────────────────────
IntentKind = Literal["question", "change", "feedback", "ingest", "decide"]


class Intent(BaseModel):
    kind: IntentKind = "question"
    instruction: str = Field("", description="The request restated as one self-contained instruction")
    document: str = Field("", description="For ingest: file name the user refers to, if any")
    decision: str = Field("", description="approve_all | reject_all | approve_some | answer_question, or empty")
    op_indexes: List[int] = Field(default_factory=list, description="For approve_some: 1-based indexes of the operations")
    language: str = Field("es", description="ISO 639-1 of the user's message")


class TextOutput(BaseModel):
    text: str = ""


# ── member updates (api/services/member_updates.py) ───────────────────────
class UpdateItem(BaseModel):
    """One thing a member reported. Recorded, never applied by itself."""
    type: str = Field("", description="done | progress | not_started | blocker | date_change | plan | idea | question | hearsay")
    task_id: str = Field("", description="taskId it is about, empty when it is about no existing task")
    text: str = Field("", description="What the member means, one sentence, in the project's language")
    owner: str = Field("", description="Who must act (blockers, plans): participant or contact name")
    due_date: str = Field("", description="YYYY-MM-DD when a date was given or implied")
    certainty: str = Field("", description="sure | estimated | conditional | low")
    depends_on: str = Field("", description="taskId this depends on (conditions, 'depende de ...')")
    about_person: str = Field("", description="hearsay: whose work it is about")
    decision_for_owner: bool = Field(False, description="An idea/proposal only the owner can decide")
    urgent: bool = Field(False, description="Blocker that depends on the project owner, or due within 2 days")
    quote: str = Field("", description="The member's own words, max 15 words")


class UpdateTurn(BaseModel):
    reply: str = Field("", description="What to answer the member now (questions or the closing summary)")
    items: List[UpdateItem] = Field(default_factory=list, description="The FULL current list of recorded items")
    finished: bool = Field(False, description="True when the update is complete")


class UpdatePrep(BaseModel):
    """The questions the AI prepares for a member's next update (email + chat opening)."""
    subject: str = Field("", description="Email subject, short, in the member's language")
    greeting: str = Field("", description="One friendly line that opens the email and the chat")
    questions: List[str] = Field(default_factory=list, description="2-3 concrete questions about their pending work")
