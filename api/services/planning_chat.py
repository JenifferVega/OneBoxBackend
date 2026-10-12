"""Planning chat (Intelligence > Replanificación): move a whole project at once.

The owner does two things here:
  * TALK: a plain message that asks something ("¿qué está bloqueado?", "¿por
    qué cerraste la tarea de Meta?") gets an answer built from the tasks, the
    plan analysis and the record of earlier proposals (reasons + evidence).
    No proposal, nothing changes.
  * REPLAN with new context -- pasted text, a document, the project's Trello
    board, or a message that orders or reports a change ("Rafa se va, Claudia
    asume diseño"). The planning graph returns a PROPOSAL.
A plain message is classified first (interpret_request); attachments always replan.
Approving / rejecting is done with the proposal's buttons, not by chatting.
The daily digest of the members' updates (updates_consolidation.py) also
lands here, with its own proposal. Nothing is written until the leader
approves all of it or a part; an applied proposal can be reverted.

Storage: table onebox-planning (projectId + itemId)
    msg#<iso>#<rand>   a chat message
    prop#<id>          a proposal: status pending | applied | partially_applied |
                       rejected | reverted | stale | superseded
Runs take minutes, so each message starts a background job; the page polls
GET .../planning/jobs/{jobId} for the step it is on.
"""
import json
import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from boto3.dynamodb.conditions import Key
from fastapi import HTTPException

from api.services import planning_live

TABLE_NAME = "onebox-planning"
TODAY = ""          # tests pin the date ("YYYY-MM-DD"); empty = today
_table = None
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
PROPOSAL_KEYS = ("proposalId", "projectId", "basePlanVersion", "trigger", "createdAt", "summary", "counts",
                 "operations", "questions", "notes", "shifts", "warnings", "unresolved_errors", "facts")
MAX_FACTS = 200             # facts read from the sources, kept with the proposal for the chat


def _compact_facts(facts) -> list:
    """What the replan read in the sources (one line each + its quote), so the
    planning chat can answer "what was said about X" without reading the
    files again. ~60 facts of a meeting transcript are ~20 KB."""
    out = []
    for f in (facts or [])[:MAX_FACTS]:
        ev = (f.get("evidence") or [{}])[0] or {}
        out.append({k: v for k, v in {
            "kind": f.get("kind", ""), "text": (f.get("text") or "")[:300], "who": f.get("who", ""),
            "date": f.get("date", ""), "source": ev.get("source", ""), "locator": ev.get("locator", ""),
            "quote": (ev.get("quote") or "")[:200]}.items() if v})
    return out


def table():
    global _table
    if _table is None:
        from api.deps import dynamodb
        _table = dynamodb.Table(TABLE_NAME)
    return _table


def _now() -> str:
    return datetime.utcnow().isoformat()


def _dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=lambda o: int(o) if isinstance(o, Decimal) and o == int(o)
                      else float(o) if isinstance(o, Decimal) else str(o))


def _loads(s: str):
    return json.loads(s or "null", parse_float=Decimal)


def _plain(x):
    """DynamoDB Decimals -> int/float, for JSON responses."""
    return json.loads(_dumps(x))


def _access(uid: str, email: str, project_id: str) -> dict:
    from api.services.access import has_project_access
    has, _owner, proj = has_project_access(uid, email, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
    return proj


def _owner_access(uid: str, email: str, project_id: str) -> dict:
    """Deciding on the plan is the owner's. A member could approve the
    proposal built from their OWN status update, which made approval mean
    nothing."""
    proj = _access(uid, email, project_id)
    if proj.get("userId") != uid:
        raise HTTPException(status_code=403, detail="Only the project owner can decide on proposals")
    return proj


def _items(project_id: str) -> list:
    out, last = [], None
    while True:
        args = {"KeyConditionExpression": Key("projectId").eq(project_id)}
        if last:
            args["ExclusiveStartKey"] = last
        res = table().query(**args)
        out += res.get("Items", [])
        last = res.get("LastEvaluatedKey")
        if not last:
            return out


def _put_message(project_id: str, role: str, content: str, **extra) -> dict:
    stamp = _now()
    item = {"projectId": project_id, "itemId": f"msg#{stamp}#{uuid.uuid4().hex[:6]}", "role": role,
            "content": content or "", "createdAt": stamp, **{k: v for k, v in extra.items() if v not in (None, "")}}
    table().put_item(Item=item)
    return item


def _proposal_item(project_id: str, prop_id: str) -> dict:
    item = table().get_item(Key={"projectId": project_id, "itemId": f"prop#{prop_id}"}).get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return item


def _set(project_id: str, item_id: str, **fields) -> None:
    names = {f"#k{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    table().update_item(Key={"projectId": project_id, "itemId": item_id},
                        UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(fields))),
                        ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    if item_id.startswith("prop#") and "status" in fields:
        refresh_pending_marker(project_id)


def refresh_pending_marker(project_id: str) -> None:
    """Keep `pendingProposal` on the project item: the project list shows
    "proposal waiting" without reading every project's planning chat.
    Written whenever a proposal is saved, decided, replaced or goes stale."""
    try:
        from agent.tools import projects_table
        item = pending_proposal(project_id)
        marker = None if not item else {
            "proposalId": item.get("proposalId", ""), "createdAt": item.get("createdAt", ""),
            "source": item.get("source", ""), "summary": (item.get("summary") or "")[:300],
            "counts": _loads(_dumps(item.get("counts") or {}))}
        projects_table.update_item(Key={"projectId": project_id}, UpdateExpression="SET pendingProposal = :p",
                                   ExpressionAttributeValues={":p": marker})
    except Exception as e:
        print(f"[planning_chat] pending marker not updated for {project_id}: {e}")


def pending_proposal(project_id: str) -> Optional[dict]:
    props = [i for i in _items(project_id) if i["itemId"].startswith("prop#") and i.get("status") == "pending"]
    if not props:
        return None
    props.sort(key=lambda i: i.get("createdAt", ""))
    return props[-1]


def save_proposal(project_id: str, proposal: dict, source: str, actor: str = "", log_key: str = "") -> dict:
    """Store a proposal as THE pending one of the project (older pending ones
    become 'superseded': only one plan change is open at a time)."""
    for old in [i for i in _items(project_id) if i["itemId"].startswith("prop#") and i.get("status") == "pending"]:
        _set(project_id, old["itemId"], status="superseded", decidedAt=_now())
    compact = {k: proposal.get(k) for k in PROPOSAL_KEYS if k in proposal}
    if "facts" in compact:
        compact["facts"] = _compact_facts(compact["facts"])
    pid = proposal.get("proposalId") or uuid.uuid4().hex[:12]
    item = {"projectId": project_id, "itemId": f"prop#{pid}", "proposalId": pid, "status": "pending",
            "source": source, "createdAt": _now(), "createdBy": actor,
            "basePlanVersion": int(proposal.get("basePlanVersion") or 0),
            "summary": (proposal.get("summary") or "")[:1000], "counts": _loads(_dumps(proposal.get("counts") or {})),
            "proposal": _dumps(compact), "log": log_key}
    table().put_item(Item=item)
    refresh_pending_marker(project_id)
    return item


def merge_or_save_proposal(project_id: str, proposal: dict, source: str, actor: str = "") -> dict:
    """Check-ins of several people must not cancel each other: when a pending
    proposal exists on the same plan version, the new operations and notes
    are appended to it instead of superseding it."""
    item = pending_proposal(project_id)
    if not item or int(item.get("basePlanVersion") or 0) != int(proposal.get("basePlanVersion") or 0):
        return save_proposal(project_id, proposal, source, actor=actor)
    cur = _plain(_loads(item.get("proposal")) or {})
    cur["operations"] = (cur.get("operations") or []) + (proposal.get("operations") or [])
    cur["notes"] = (cur.get("notes") or []) + (proposal.get("notes") or [])
    cur["questions"] = (cur.get("questions") or []) + (proposal.get("questions") or [])
    cur["facts"] = ((cur.get("facts") or []) + _compact_facts(proposal.get("facts")))[:MAX_FACTS]
    counts = {}
    for o in cur["operations"]:
        counts[o["op"]] = counts.get(o["op"], 0) + 1
    cur["counts"] = counts
    summary = ((item.get("summary") or "") + " + " + (proposal.get("summary") or "")).strip(" +")[:1000]
    cur["summary"] = summary
    sources = sorted(set((item.get("source") or "").split(" + ")) | {source})
    _set(project_id, item["itemId"], proposal=_dumps(cur), counts=_loads(_dumps(counts)), summary=summary,
         source=" + ".join(s for s in sources if s))
    return {**item, "proposalId": item["proposalId"]}


def _public_proposal(item: dict) -> dict:
    out = {k: item.get(k) for k in ("proposalId", "status", "source", "createdAt", "createdBy", "summary", "counts",
                                    "decidedAt", "decidedBy", "approved", "basePlanVersion", "appliedPlanVersion")}
    p = _loads(item.get("proposal")) or {}
    out.update(operations=p.get("operations") or [], questions=p.get("questions") or [],
               notes=p.get("notes") or [], warnings=p.get("warnings") or [],
               trello=_loads(item.get("trello")) if item.get("trello") else None)
    return _plain(out)


def _describe(proposal: Optional[dict]) -> str:
    if not proposal:
        return "No encontré cambios que proponer."
    c = proposal.get("counts") or {}
    label = {"create": "nuevas", "update": "actualizadas", "close": "cerradas", "delete": "eliminadas",
             "block": "bloqueadas", "reopen": "reabiertas", "link": "dependencias"}
    parts = [f"{n} {label.get(k, k)}" for k, n in c.items() if n]
    txt = "Propuesta: " + (", ".join(parts) or "sin cambios en tareas")
    if proposal.get("notes"):
        txt += f", {len(proposal['notes'])} notas"
    if proposal.get("questions"):
        txt += f". {len(proposal['questions'])} preguntas para ti"
    return txt + "."


# ── context ─────────────────────────────────────────────────────────────────

def trello_text(uid: str, proj: dict) -> Tuple[str, str]:
    """(name, text) of the project's Trello board: one line per open card,
    the list is the status. Read-only: OneBox stays the source of truth."""
    from api.services import trello as trello_service
    board = proj.get("trelloBoardId")
    if not board:
        raise HTTPException(status_code=400, detail="This project has no Trello board linked")
    lines = [f"Tablero de Trello del proyecto {proj.get('name', '')} (leído {_now()[:16]}). "
             "Cada línea: [lista] tarjeta — vence — descripción."]
    for lst in trello_service.list_board_lists(uid, board):
        for c in trello_service.list_cards(uid, lst["listId"]):
            if c.get("closed"):
                continue
            desc = " ".join((c.get("desc") or "").split())[:300]
            lines.append(f"[{lst['name']}] {c['name']}" + (f" — vence {c['due']}" if c.get("due") else "")
                         + (f" — {desc}" if desc else ""))
    return f"trello-{_now()[:16].replace(':', '')}.txt", "\n".join(lines)


def _store_source(proj: dict, uid: str, email: str, name: str, text: str, raw: Optional[bytes] = None,
                  ext: str = "txt", content_type: str = "text/plain") -> None:
    """Every context becomes a project attachment, so later analyses see it too."""
    from agent.document_parser import upload_to_s3
    from api.services.documents import save_attachment_record
    data = raw if raw is not None else text.encode("utf-8")
    key = upload_to_s3(data, proj["projectId"], name, content_type)
    save_attachment_record(project_id=proj["projectId"], user_id=proj.get("userId", uid), file_name=name,
                           file_size=len(data), content_type=content_type, ext=ext, s3_key=key,
                           extracted_text=text, source="planning-chat", uploaded_by=uid,
                           uploaded_by_email=(email or "").lower())


# ── jobs ────────────────────────────────────────────────────────────────────

def new_job(project_id: str, source: str = "") -> dict:
    """A running job the planning panel can follow (step by step). Also used
    by replans started outside the chat (an upload), so the panel shows them."""
    job = {"jobId": uuid.uuid4().hex[:12], "projectId": project_id, "status": "running", "step": "",
           "steps": [], "started": time.time(), "error": "", "messageId": "", "proposalId": "", "source": source}
    with _JOBS_LOCK:
        _JOBS[job["jobId"]] = job
    return job


def _running(project_id: str) -> Optional[dict]:
    with _JOBS_LOCK:
        return next((j for j in _JOBS.values() if j["projectId"] == project_id and j["status"] == "running"), None)


def job_status(uid: str, email: str, project_id: str, job_id: str) -> dict:
    _access(uid, email, project_id)
    job = _JOBS.get(job_id)
    if not job or job["projectId"] != project_id:
        raise HTTPException(status_code=404, detail="Job not found (the server may have restarted)")
    return {k: job.get(k) for k in ("jobId", "status", "step", "steps", "error", "messageId", "proposalId")} | {
        "seconds": int(time.time() - job["started"])}


def start_message(uid: str, email: str, project_id: str, message: str, context_text: str = "",
                  file: Optional[Tuple[bytes, str, str]] = None, include_trello: bool = False,
                  reread_all: bool = False, run_async: bool = True, llm=None, replan: bool = False) -> dict:
    """replan=True: the leader pressed "prepare a proposal with this": the
    message is the context, no classification."""
    proj = _access(uid, email, project_id)
    from api.services.projects import ensure_open
    ensure_open(proj)
    message, context_text = (message or "").strip(), (context_text or "").strip()
    if not message and not context_text and not file and not include_trello:
        raise HTTPException(status_code=400, detail="Write an instruction or add context")
    if _running(project_id):
        raise HTTPException(status_code=409, detail="A replanning run is already in progress for this project")
    stamp = _now()[:16].replace(":", "")
    sources: List[Tuple[str, str]] = []
    if context_text:
        name = f"contexto-{stamp}.txt"
        _store_source(proj, uid, email, name, context_text)
        sources.append((name, context_text))
    if file:
        from agent.document_parser import extract_text, validate_file
        data, fname, ctype = file
        ok, ext, err = validate_file(data, fname or "", ctype or "")
        if not ok:
            raise HTTPException(status_code=400, detail=err)
        text = extract_text(data, ext)
        if not text or len(text.strip()) < 20:
            raise HTTPException(status_code=400, detail="Could not extract text from the document")
        _store_source(proj, uid, email, fname, text, raw=data, ext=ext, content_type=ctype or "")
        sources.append((fname, text))
    if include_trello:
        # The board is read with the OWNER's connection: members have none.
        name, text = trello_text(proj.get("userId") or uid, proj)
        _store_source(proj, uid, email, name, text)
        sources.append((name, text))
    # A plain message (no attachments) is CONVERSATION by default: _run_job
    # asks the model first and replans only on an explicit order. It becomes
    # a source only when it is replanned with.
    talk = not sources and not reread_all and not replan
    if not sources and not reread_all and len(message) < 3:
        raise HTTPException(status_code=400, detail="Write a question or explain the change")
    if replan and not sources and not reread_all:
        name = f"contexto-{stamp}.txt"
        _store_source(proj, uid, email, name, message)
        sources.append((name, message))
    user_msg = _put_message(project_id, "user", message or "(contexto adjunto)", userId=uid, userEmail=email,
                            sources=[n for n, _t in sources] or None, rereadAll=reread_all or None)
    job = new_job(project_id, "chat")
    args = (job, uid, email, proj, message, sources, reread_all, llm, talk, stamp)
    if run_async:
        threading.Thread(target=_run_job, args=args, daemon=True, name=f"replan-{project_id}").start()
    else:
        _run_job(*args)
    return {"jobId": job["jobId"], "messageId": user_msg["itemId"]}


def _run_job(job, uid, email, proj, message, sources, reread_all, llm=None, talk=False, stamp=""):
    from agent.planning.runner import run_planning
    pid = proj["projectId"]

    def step(node):
        job["step"] = node
        job["steps"].append(node)

    try:
        if talk:
            if _talk(job, pid, message, llm, step):
                return
            # It orders or reports a change: the message is the context.
            name = f"contexto-{stamp or _now()[:16].replace(':', '')}.txt"
            _store_source(proj, uid, email, name, message)
            sources = [(name, message)]
        export = planning_live.snapshot(pid, sources, read_all=reread_all)
        # Approving or rejecting is done with the buttons of the proposal.
        job["intent"] = job.get("intent") or {"kind": "ingest", "instruction": message}
        res = run_planning(export, "reanalyze" if reread_all else "document", message=message, today=TODAY,
                           documents=None if reread_all else [n for n, _t in sources],
                           mode="full" if reread_all else "delta", llm=llm, on_step=step)
        proposal = res.get("proposal")
        prop_item = None
        worth = proposal and (proposal.get("operations") or proposal.get("questions")
                              or (proposal.get("notes") and not talk))
        # From a typed message, notes alone are an echo of the message
        # ("no changes were made...") -- not a proposal worth approving.
        if worth:
            log = ""
            try:
                log = planning_live._log_to_s3(pid, _now(), {"source": "planning-chat", "message": message,
                                                             "proposal": proposal, "trace": res.get("trace")})
            except Exception as e:
                print(f"[planning_chat] S3 log error: {e}")
            prop_item = save_proposal(pid, proposal, "chat", actor=email or uid, log_key=log)
            job["proposalId"] = prop_item["proposalId"]
        job["trace"] = [t.get("node") for t in res.get("trace") or []]
        if talk and not prop_item:
            # Nothing to propose from a typed message: talk about it instead
            # of a bare "no changes".
            _answer(job, pid, message, llm, step, intent=job.get("intent") or {})
            return
        text = res.get("answer") or (res.get("narration") if prop_item else "") or _describe(proposal if prop_item else None)
        msg = _put_message(pid, "assistant", text, proposalId=prop_item["proposalId"] if prop_item else None)
        job["messageId"] = msg["itemId"]
        job["status"] = "done"
    except HTTPException as e:       # e.g. a stale proposal: decide() already told the chat
        job["status"], job["error"] = "done", str(e.detail)[:300]
    except Exception as e:
        import traceback
        traceback.print_exc()
        job["status"], job["error"] = "error", str(e)[:300]
        try:
            _put_message(pid, "assistant", f"No pude completar la replanificación: {str(e)[:200]}", error=True)
        except Exception:
            pass


# ── talk: questions about the plan and about earlier AI decisions ──────────
TALK_PROPOSALS = 6          # earlier proposals shown to the model
TALK_HISTORY = 10           # chat messages shown to the model


def _history(items: list, limit: int = TALK_HISTORY) -> List[dict]:
    msgs = sorted((i for i in items if i["itemId"].startswith("msg#")), key=lambda i: i["itemId"])
    # The last one is the message being answered.
    return [{"role": m.get("role"), "content": m.get("content", "")} for m in msgs[:-1][-limit:]]


def _proposals_block(items: list, task_text: dict) -> str:
    props = sorted((i for i in items if i["itemId"].startswith("prop#")), key=lambda i: i.get("createdAt", ""))
    out = []
    for item in props[-TALK_PROPOSALS:]:
        p = _plain(_loads(item.get("proposal")) or {})
        is_open = item.get("status") == "pending"
        head = (("- OPEN PROPOSAL, the one the leader is reviewing now: " if is_open else "- ") + f"Proposal {item.get('proposalId')} ({(item.get('createdAt') or '')[:10]}, from {item.get('source') or '?'}, "
                f"status {item.get('status')}"
                + (f", decided by {item.get('decidedBy')}" if item.get("decidedBy") else "")
                + (f", approved ops {item.get('approved')}" if item.get("approved") else "") + ")")
        out.append(head + (f": {item.get('summary')[:400]}" if item.get("summary") else ""))
        for n, o in enumerate(p.get("operations") or [], 1):
            title = (o.get("text") or task_text.get(o.get("task_id", ""), ""))[:120]
            changes = ", ".join(f"{k}={o[k]}" for k in ("status", "assigned_to", "start_date", "due_date",
                                                         "blocked_reason", "duplicate_of") if o.get(k))
            line = f"    {n}. {o.get('op')} {o.get('task_id', '')} '{title}'"
            if o.get("date_status") == "estimated" and (o.get("due_date") or o.get("start_date")):
                changes += ", dates estimated (not agreed)"
            line += f" [{changes.strip(', ')}]" if changes else ""
            line += f" (confidence {o['confidence']})" if o.get("confidence") and o["confidence"] != "medium" else ""
            line += f" -- reason: {o['reason'][:250]}" if o.get("reason") else ""
            ev = "; ".join(f"{e.get('source', '')} {e.get('locator', '')}: \"{(e.get('quote') or '')[:160]}\""
                           for e in (o.get("evidence") or [])[:2])
            out.append(line + (f" -- evidence: {ev}" if ev else ""))
        for q in p.get("questions") or []:
            out.append(f"    question: {(q.get('text') or '')[:250]}")
        for nt in (p.get("notes") or [])[:8]:
            out.append(f"    note: {(nt.get('text') or '')[:250]}")
    return "\n".join(out) or "(no proposals yet)"


# Fixed texts of the conversational reply, in the leader's language (the
# button label must match the one the interface shows: locales/*.json
# intelligence.planning.replanWith).
_TALK_TEXT = {
    "es": {"failed": "No pude responder ahora; intenta de nuevo en un momento.",
           "buttons": "(Para aprobar o rechazar una propuesta usa sus botones.)",
           "button": "Preparar una propuesta de plan con esto"},
    "en": {"failed": "I could not answer right now; please try again in a moment.",
           "buttons": "(To approve or reject a proposal, use its buttons.)",
           "button": "Prepare a plan proposal with this"},
}
TALK_FACT_PROPOSALS = 3     # proposals whose facts the chat sees
TALK_FACTS = 150


def _facts_block(items: list, question: str = "") -> str:
    """Facts the last replans read in their sources, newest proposal first;
    those that share words with the question go on top."""
    from agent.planning.sources import norm
    import re
    props = sorted((i for i in items if i["itemId"].startswith("prop#")), key=lambda i: i.get("createdAt", ""))
    words = {norm(w) for w in re.findall(r"[\wáéíóúñü]+", question.lower()) if len(w) >= 4 and w not in _STOP}
    rows, seen = [], set()
    for item in reversed(props[-TALK_FACT_PROPOSALS:]):
        p = _plain(_loads(item.get("proposal")) or {})
        for f in p.get("facts") or []:
            key = norm(f.get("text", ""))[:120]
            if not key or key in seen:
                continue
            seen.add(key)
            line = (f"- [{f.get('kind', '')}] {f.get('date') or ''} {f.get('text', '')}"
                    + (f" ({f['who']})" if f.get("who") else "")
                    + (f" -- {f.get('source', '')} {f.get('locator', '')}: \"{f['quote']}\"" if f.get("quote") else ""))
            hit = sum(1 for w in words if w in norm(line))
            rows.append((hit, len(rows), line))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return "\n".join(r[2] for r in rows[:TALK_FACTS]) or "(no facts saved: proposals made before facts were kept)"


def _notes_block(insights: list) -> str:
    rows = sorted(insights or [], key=lambda i: i.get("createdAt", ""))[-20:]
    return "\n".join(f"- {(i.get('createdAt') or '')[:10]} {(i.get('text') or i.get('content') or i.get('title') or '')[:250]}"
                     for i in rows) or "(none)"


_STOP = {"que", "qué", "quien", "quién", "como", "cómo", "cual", "cuál", "cuales", "donde", "cuando", "para",
         "por", "porque", "con", "los", "las", "del", "una", "uno", "unos", "este", "esta", "esto", "ese", "eso",
         "sucede", "pasa", "paso", "pasó", "hay", "tiene", "tienen", "sobre", "the", "what", "why", "who", "which",
         "with", "about", "does", "did", "happens", "happened", "está", "esta", "están", "estan", "tareas", "tarea"}


def _excerpts(snap: dict, question: str, limit: int = 25, width: int = 300) -> str:
    """Lines of the project's documents that mention the question's words
    ("error 404" finds where the 404 was talked about). Plain matching, no
    LLM: the newest lines first, with the source name."""
    from agent.planning.sources import norm
    import re
    words = {norm(w) for w in re.findall(r"[\wáéíóúñü]+", question.lower())
             if (len(w) >= 3 or w.isdigit()) and w not in _STOP}
    if not words:
        return "(none)"
    hits = []
    for src in snap.get("sources") or []:
        for line in (src.get("text") or "").splitlines():
            n = norm(line)
            score = sum(1 for w in words if w in n)
            if score:
                hits.append((score, src.get("createdAt") or "", src.get("name", ""), line.strip()[:width]))
    hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
    return "\n".join(f"{name}: {line}" for _s, _d, name, line in hits[:limit]) or "(no document mentions it)"


def _talk(job: dict, pid: str, message: str, llm, step) -> bool:
    """Answer a question in the planning chat. Returns False when the message
    orders or reports a change: the caller replans with it as context."""
    from agent.planning import nodes
    from agent.planning.runner import default_llm

    llm = llm or default_llm()
    items = _items(pid)
    export = planning_live.snapshot(pid)
    if export is None:
        raise HTTPException(status_code=404, detail="Project not found")
    history = _history(items)
    state = {"trigger": "chat", "today": TODAY or datetime.utcnow().date().isoformat(), "export": export,
             "message": message, "history": history, "trace": []}
    state.update(nodes.load_project(state))
    step("interpret_request")
    pending = pending_proposal(pid)
    state["open_proposal"] = _plain(_loads(pending.get("proposal")) or {}) if pending else None
    intent = nodes.interpret_request(state, llm)["intent"]
    job["intent"] = intent
    if intent.get("kind") in ("change", "ingest") and intent.get("decision") != "answer_question":
        return False      # an explicit order: replan with the message
    _answer(job, pid, message, llm, step, intent=intent, state=state, items=items, history=history)
    return True


def _answer(job: dict, pid: str, message: str, llm, step, intent: dict, state: Optional[dict] = None,
            items: Optional[list] = None, history: Optional[List[dict]] = None) -> None:
    """The conversational reply: tasks, analysis, earlier proposals and
    excerpts of the documents. Never proposes."""
    from agent.planning import nodes, prompts
    from agent.planning.runner import default_llm
    from agent.planning.schedule import analyze_plan
    from agent.planning.schemas import TextOutput

    llm = llm or default_llm()
    if state is None:
        export = planning_live.snapshot(pid)
        if export is None:
            raise HTTPException(status_code=404, detail="Project not found")
        state = {"trigger": "chat", "today": TODAY or datetime.utcnow().date().isoformat(), "export": export,
                 "message": message, "trace": []}
        state.update(nodes.load_project(state))
    items = items if items is not None else _items(pid)
    history = history if history is not None else _history(items)
    step("analyze_plan")
    snap: dict = state["snapshot"]  # type: ignore[assignment]
    analysis = analyze_plan(snap["tasks"], snap["project"], snap["participants"], nodes._today(state),
                            state.get("aliases"))
    step("answer")
    task_text = {t["taskId"]: t.get("text", "") for t in snap["tasks"]}
    hist = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in history) or "(empty)"
    lang = (intent.get("language") or "es").lower()[:2]
    system = nodes._fill(prompts.CONVERSE, language=lang, button=_TALK_TEXT["en" if lang == "en" else "es"]["button"],
                         project=nodes._project_line(snap), today=state["today"],
                         people=nodes._participants_line(snap), tasks=nodes._tasks_block(snap["tasks"]),
                         analysis=json.dumps(analysis, ensure_ascii=False, default=str)[:15000],
                         proposals=_proposals_block(items, task_text)[:20000],
                         notes=_notes_block(snap.get("insights")),
                         documents=", ".join(s["name"] for s in snap["sources"]) or "none",
                         facts=_facts_block(items, message),
                         excerpts=_excerpts(snap, message),
                         history=hist, question=message)
    out, err = nodes.call_structured(llm, TextOutput, system, is_empty=lambda o: not o.text.strip())
    words = _TALK_TEXT["en" if lang == "en" else "es"]
    text = (out.text or "").strip() or words["failed"]
    if intent.get("kind") == "decide":
        text += "\n\n" + words["buttons"]
    # Reported facts (or an answer to a proposal question) may deserve a
    # proposal: the reply carries a button that replans with the message.
    offer = intent.get("kind") in ("feedback", "change") or intent.get("decision") == "answer_question"
    msg = _put_message(pid, "assistant", text, kind="answer", replanText=message if offer else None)
    job["trace"] = ["interpret_request", "analyze_plan", "answer"]
    job["messageId"], job["status"] = msg["itemId"], "done"
    if err:
        job["error"] = str(err)[:300]
    return True


# ── decide / revert ─────────────────────────────────────────────────────────

def decide(uid: str, email: str, project_id: str, prop_id: str, decision: str,
           op_indexes: Optional[List[int]] = None, _checked: bool = False) -> dict:
    from agent.planning.schedule import apply_proposal
    from agent.tools import insights_table, projects_table, tasks_table
    proj = _owner_access(uid, email, project_id) if not _checked else None
    if proj is not None:
        from api.services.projects import ensure_open
        ensure_open(proj)
    item = _proposal_item(project_id, prop_id)
    if item.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Proposal is {item.get('status')}, not pending")
    now = _now()
    if decision == "reject_all":
        _set(project_id, item["itemId"], status="rejected", decidedAt=now, decidedBy=email or uid)
        msg = _put_message(project_id, "assistant", "Propuesta rechazada. No se cambió nada.", proposalId=prop_id)
        return {"status": "rejected", "messageId": msg["itemId"]}
    proposal = _plain(_loads(item.get("proposal")) or {})
    before = planning_live.snapshot(project_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Project not found")
    current = int((before.get("project") or {}).get("planVersion") or 0)
    if current != int(item.get("basePlanVersion") or 0):
        _set(project_id, item["itemId"], status="stale", decidedAt=now)
        msg = _put_message(project_id, "assistant", "El plan cambió desde que se hizo esta propuesta; "
                           "pídeme que la recalcule.", proposalId=prop_id)
        raise HTTPException(status_code=409, detail="The plan changed since this proposal was made")
    n_ops = len(proposal.get("operations") or [])
    approved = None if decision == "approve_all" else sorted({int(i) for i in op_indexes or [] if 1 <= int(i) <= n_ops})
    if approved == []:
        raise HTTPException(status_code=400, detail="No operations selected")
    owner = (proj or before["project"]).get("userId", uid)
    after, events = apply_proposal(before, proposal, approved, actor=email or uid, now=now)
    counts = planning_live.persist(before, after, proposal, owner, now, tasks_table, insights_table, projects_table)
    idmap = counts.pop("idmap", {})
    touched = {e["taskId"] for e in events if e.get("taskId")}
    old = {t["taskId"]: t for t in before.get("tasks") or []}
    restore = {tid: old[tid] for tid in touched if tid in old}
    created = sorted(idmap[t] for t in touched if t in idmap)
    seen = {i.get("insightId") for i in before.get("insights") or []}
    note_ids = [i["insightId"] for i in after.get("insights") or [] if i.get("insightId") not in seen]
    status = "applied" if approved is None or len(approved) == n_ops else "partially_applied"
    trello = _reflect_in_trello(owner, project_id, old, touched, idmap, created)
    _count_as_reports(before, touched, now, email or uid)
    _set(project_id, item["itemId"], status=status, decidedAt=now, decidedBy=email or uid,
         approved=approved or [], appliedPlanVersion=int((after.get("project") or {}).get("planVersion") or 0),
         revert=_dumps({"restore": restore, "created": created, "notes": note_ids}), trello=_dumps(trello))
    text = (f"Aplicado: {counts.get('created', 0)} nuevas, {counts.get('updated', 0)} actualizadas, "
            f"{counts.get('deleted', 0)} eliminadas, {counts.get('notes', 0)} notas.")
    from api.services.trello_reflect import summary as trello_summary
    if trello_summary(trello):
        text += "\n" + trello_summary(trello)
    msg = _put_message(project_id, "assistant", text, proposalId=prop_id)
    return {"status": status, "counts": counts, "messageId": msg["itemId"], "trello": trello}


def _count_as_reports(before: dict, touched: set, now: str, actor: str) -> None:
    """An approved change to someone's tasks counts as their report: the owner
    updated them (typically people without an account). Their check-in clock
    restarts, so the owner is reminded again only after another period."""
    try:
        from api.services import checkin
        proj = before.get("project") or {}
        for person, tasks in checkin.assignments(proj, before.get("tasks") or []):
            if any(t["taskId"] in touched for t in tasks):
                checkin._save_state(proj["projectId"], checkin.person_key(person), lastReportAt=now,
                                    lastReportBy=f"owner:{actor}")
    except Exception as e:
        print(f"[planning_chat] check-in reset failed: {e}")


def _reflect_in_trello(owner_uid: str, project_id: str, before: dict, touched: set, idmap: dict,
                       created: list) -> dict:
    """The approved change, written to the linked Trello board. Never raises:
    the app already has the change and Trello is a mirror."""
    try:
        from agent.tools import projects_table
        from api.services import trello_reflect
        proj = projects_table.get_item(Key={"projectId": project_id}).get("Item") or {}
        if not proj.get("trelloBoardId"):
            return {"status": "not_linked"}
        now_tasks = {t["taskId"]: t for t in (planning_live.snapshot(project_id) or {}).get("tasks") or []}
        ids = [idmap.get(t, t) for t in touched]
        return trello_reflect.reflect(owner_uid, proj, before, now_tasks, ids, created)
    except Exception as e:
        print(f"[planning_chat] Trello reflection failed: {e}")
        return {"status": "error", "errors": [str(e)[:200]]}


def revert(uid: str, email: str, project_id: str, prop_id: str) -> dict:
    """Put every task the proposal touched back as it was, delete what it
    created and the notes it wrote. Only the last applied proposal, and only
    while nothing else changed the plan after it."""
    from agent.tools import insights_table, projects_table, tasks_table
    proj = _owner_access(uid, email, project_id)
    item = _proposal_item(project_id, prop_id)
    if item.get("status") not in ("applied", "partially_applied"):
        raise HTTPException(status_code=409, detail="Only an applied proposal can be reverted")
    current = int(projects_table.get_item(Key={"projectId": project_id}).get("Item", {}).get("planVersion") or 0)
    if current != int(item.get("appliedPlanVersion") or -1):
        raise HTTPException(status_code=409, detail="The plan changed after this proposal; it cannot be reverted")
    data = _loads(item.get("revert")) or {}
    for tid in data.get("created") or []:
        tasks_table.delete_item(Key={"projectId": project_id, "taskId": tid})
    for tid, task in (data.get("restore") or {}).items():
        tasks_table.put_item(Item=task)
    owner = proj.get("userId", uid)
    for nid in data.get("notes") or []:
        try:
            insights_table.delete_item(Key={"userId": owner, "insightId": nid})
        except Exception as e:
            print(f"[planning_chat] note delete failed {nid}: {e}")
    now = _now()
    projects_table.update_item(Key={"projectId": project_id}, UpdateExpression="SET planVersion = :v, lastActivity = :t",
                               ExpressionAttributeValues={":v": current + 1, ":t": now})
    _set(project_id, item["itemId"], status="reverted", revertedAt=now, revertedBy=email or uid)
    msg = _put_message(project_id, "assistant", "Replanificación revertida: el plan volvió a como estaba.",
                       proposalId=prop_id)
    return {"status": "reverted", "messageId": msg["itemId"]}


# ── read ────────────────────────────────────────────────────────────────────

def get_state(uid: str, email: str, project_id: str) -> dict:
    proj = _access(uid, email, project_id)
    items = _items(project_id)
    msgs = sorted((i for i in items if i["itemId"].startswith("msg#")), key=lambda i: i["itemId"])
    props = sorted((i for i in items if i["itemId"].startswith("prop#")), key=lambda i: i.get("createdAt", ""))
    applied = [p for p in props if p.get("status") in ("applied", "partially_applied")]
    job = _running(project_id)
    public = [_public_proposal(p) for p in props[-20:]]
    # Operations on existing tasks carry only the id: send the texts along.
    from agent.tools import tasks_table
    from api.services.project_export import _query_all
    texts = {t["taskId"]: t.get("text", "") for t in
             _query_all(tasks_table, KeyConditionExpression=Key("projectId").eq(project_id))}
    return {
        "messages": _plain([{k: m.get(k) for k in ("itemId", "role", "content", "createdAt", "userEmail",
                                                    "proposalId", "sources", "error", "kind", "replanText")}
                            for m in msgs[-100:]]),
        "proposals": public,
        "taskTexts": {o["task_id"]: texts[o["task_id"]] for p in public for o in p["operations"]
                      if o.get("task_id") in texts},
        "revertible": applied[-1]["proposalId"] if applied else None,
        "job": {"jobId": job["jobId"], "step": job["step"]} if job else None,
        "trello": bool(proj.get("trelloBoardId")),
        "planVersion": int(proj.get("planVersion") or 0),
    }
