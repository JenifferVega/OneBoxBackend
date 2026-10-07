"""Nodes of the planning graph. Six call an LLM; the rest are code.

Each node returns only the keys it changes and appends one entry to `trace`,
which is what the test runner and the MCP print.
"""
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from agent.planning import prompts
from agent.planning.schedule import analyze_plan, apply_operations, apply_proposal, parse_date, propagate
from agent.planning.schemas import (CoverOutput, FactsOutput, Intent, LinkOutput, ReconcileOutput, TextOutput,
                                    TopicsOutput)
from agent.planning.sources import is_message_start, load_snapshot, norm, redact, whatsapp_delta
from agent.planning.validate import validate, validate_notes

MAX_RECONCILE_ROUNDS = 3
# Work is cut into small calls so each one answers in well under a minute:
# a 47k-character transcript in ONE call took 119 s to read and the reconcile
# that followed timed out at 180 s, three times.
CHUNK_CHARS = 12_000          # ~3k tokens of material per extraction call
TASKS_PER_BATCH = 15          # existing tasks judged per reconcile call
PARALLEL_CALLS = 4


# ── helpers ─────────────────────────────────────────────────────────────────

def _today(state) -> date:
    return datetime.strptime(state.get("today") or date.today().isoformat(), "%Y-%m-%d").date()


def _trace(state, node, t0, **notes):
    return list(state.get("trace") or []) + [
        {"node": node, "ms": int((time.perf_counter() - t0) * 1000), **notes}]


def _dump(x):
    return x.model_dump() if hasattr(x, "model_dump") else x


def _repair(schema, args):
    """Claude sometimes returns the whole answer as a JSON STRING inside one
    field ({"operations": "{\\"reviews\\": [...]}"}) or a list as a string.
    Parse those strings back; None when it still does not validate."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    fields = set(schema.model_fields)
    for k, v in list(args.items()):
        if not (isinstance(v, str) and v.strip()[:1] in ("{", "[")):
            continue
        try:
            j = json.loads(v)
        except ValueError:
            continue
        if isinstance(j, dict) and set(j) & fields:
            args = {**{a: b for a, b in args.items() if a != k}, **j}
        else:
            args[k] = j
    try:
        return schema.model_validate(args)
    except Exception:
        return None


def call_structured(llm, schema, system: str, is_empty: Callable = lambda o: False,
                    retries: int = 1):
    """One structured call with retries. A schema-valid EMPTY answer counts as
    a failure (every field has a default, and Gemini returns `{}` readily).
    With include_raw the raw tool call is kept, so a malformed answer can be
    repaired instead of thrown away (and paid for again)."""
    try:
        structured = llm.with_structured_output(schema, include_raw=True)
    except TypeError:
        structured = llm.with_structured_output(schema)
    last_err = ""
    for _ in range(retries + 1):
        try:
            res = structured.invoke([SystemMessage(content=system),
                                     HumanMessage(content="Return the structured output now.")])
            out = res
            if isinstance(res, dict) and "parsed" in res:
                out = res.get("parsed")
                if out is None:
                    raw = res.get("raw")
                    calls = getattr(raw, "tool_calls", None) or []
                    out = _repair(schema, calls[0].get("args")) if calls else None
                    if out is None:
                        last_err = ("unparseable output: " + str(res.get("parsing_error") or "")[:250])
                        continue
            if out is not None and not is_empty(out):
                return out, ""
            last_err = "empty output"
        except Exception as e:  # provider error, parse error, timeout
            # Provider errors can echo the request URL, API key included
            # (Gemini puts it in the query string). Never let one reach a log
            # or a run.json.
            last_err = re.sub(r"(key|api[_-]?key|token)=[^&\s'\"]+", r"\1=<redacted>", str(e), flags=re.I)[:300]
    return schema(), last_err


def _fill(template: str, **kw) -> str:
    for k, v in kw.items():
        template = template.replace("{" + k + "}", v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    return template


def _project_line(snap) -> str:
    p = snap["project"]
    return (f"{p.get('name','')} — {p.get('description','')} (type: {p.get('type','')}, "
            f"delivery: {p.get('deliveryDate') or 'none'}, status: {p.get('status','')})")


def _participants_line(snap) -> str:
    return "; ".join(f"{p['name']} ({p.get('role') or '-'}, {p.get('email') or 'no email'})"
                     for p in snap["participants"]) or "none"


def _tasks_block(tasks) -> str:
    lines = []
    for t in tasks:
        dates = f"{t.get('startDate') or '-'}..{t.get('dueDate') or '-'}"
        if t.get("dateStatus"):
            dates += f" [{t['dateStatus']}]"
        lines.append(" | ".join([t["taskId"], t.get("status", ""), t.get("assignedTo") or "-", dates,
                                 t.get("createdBy") or "-", ",".join(t.get("dependsOn") or []) or "-",
                                 (t.get("text") or "").replace("\n", " ")[:220]]))
    return "\n".join(lines) or "(no tasks)"


def _chunks(text: str):
    """Split material into ~CHUNK_CHARS pieces on natural boundaries: a new
    WhatsApp message, or a blank line (paragraph / transcript turn / page)."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    blocks, cur = [], []
    for line in text.splitlines(keepends=True):
        boundary = is_message_start(line) or not line.strip()
        if boundary and cur:
            blocks.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("".join(cur))
    out, buf = [], ""
    for b in blocks:
        if buf and len(buf) + len(b) > CHUNK_CHARS:
            out.append(buf)
            buf = ""
        buf += b
        # A block with no boundary inside (a document without blank lines)
        # is cut at line ends, so no chunk grows past ~CHUNK_CHARS.
        while len(buf) > CHUNK_CHARS * 1.25:
            cut = buf.rfind("\n", 0, CHUNK_CHARS)
            cut = cut + 1 if cut > 0 else CHUNK_CHARS
            out.append(buf[:cut])
            buf = buf[cut:]
    if buf:
        out.append(buf)
    return out


# ── code nodes ──────────────────────────────────────────────────────────────

def load_project(state):
    t0 = time.perf_counter()
    snap = load_snapshot(state.get("export") or {})
    aliases = {}
    for p in snap["participants"]:
        aliases[norm(p["name"])] = p["name"]
        if p.get("email"):
            aliases[norm(p["email"])] = p["name"]
    return {"snapshot": snap, "aliases": aliases, "iteration": 0,
            "trace": _trace(state, "load_project", t0, tasks=len(snap["tasks"]),
                            sources=len(snap["sources"]))}


def load_sources(state):
    """The text to read: the requested document (only its new part when it is
    a WhatsApp export already partly analysed) or every document (reanalyze)."""
    t0 = time.perf_counter()
    snap = state["snapshot"]
    srcs = snap["sources"]
    wanted = (state.get("document") or (state.get("intent") or {}).get("document") or "").strip()
    mode = state.get("mode") or ("full" if state.get("trigger") == "reanalyze" else "delta")
    material, notes = [], []

    if state.get("documents") is not None:
        names = {norm(n) for n in state["documents"]}
        chosen = [i for i, s in enumerate(srcs) if norm(s["name"]) in names]
        missing = names - {norm(srcs[i]["name"]) for i in chosen}
        notes += [f"document '{n}' not found" for n in sorted(missing)]
    elif wanted:
        idx = next((i for i in range(len(srcs) - 1, -1, -1)
                    if norm(srcs[i]["name"]) == norm(wanted) or norm(wanted) in norm(srcs[i]["name"])), None)
        chosen = [idx] if idx is not None else []
        if idx is None:
            notes.append(f"document '{wanted}' not found")
    else:
        chosen = list(range(len(srcs)))

    for i in chosen:
        s = srcs[i]
        text, meta = s["text"], {"kind": s["kind"]}
        if s["kind"] == "whatsapp":
            earlier = [o["text"] for o in srcs[:i] if o["kind"] == "whatsapp"]
            if mode == "delta" and earlier:
                text, meta = whatsapp_delta(text, earlier)
                meta["kind"] = "whatsapp"
            elif mode == "full" and not wanted:
                # Every export repeats the whole history: read only the newest.
                later = [o for o in srcs[i + 1:] if o["kind"] == "whatsapp"]
                if later:
                    notes.append(f"{s['name']} skipped: contained in a later export")
                    continue
        text, n = redact(text)
        meta["redacted"] = n
        material.append({"name": s["name"], "text": text, "meta": meta})
    return {"material": material,
            "trace": _trace(state, "load_sources", t0, read=[(m["name"], len(m["text"]), m["meta"]) for m in material],
                            notes=notes)}


def analyze(state):
    t0 = time.perf_counter()
    snap = state["snapshot"]
    an = analyze_plan(snap["tasks"], snap["project"], snap["participants"], _today(state), state.get("aliases"))
    upd = {"analysis": an, "trace": _trace(state, "analyze_plan", t0, incongruences=len(an["incongruences"]))}
    if state.get("trigger") in ("cron", "task_event"):
        relevant = [i for i in an["incongruences"]
                    if i["kind"] in ("overdue", "not_started", "after_delivery", "child_after_parent",
                                     "blocked_without_reason", "dependency_cycle", "invalid_delivery_date")]
        upd["incongruences_in"] = relevant
        upd["instruction"] = state.get("message") or (
            "Revisión automática: resolver las incoherencias del cronograma. Para fechas estimadas vencidas "
            "no inventes otra fecha: pregunta al líder.")
    return upd


def validate_node(state):
    t0 = time.perf_counter()
    snap = state["snapshot"]
    known = list(state.get("people") or [])
    v = validate(snap["tasks"], state.get("operations") or [], snap["participants"], known)
    notes, note_warnings = validate_notes(snap["tasks"], v["operations"], state.get("notes") or [])
    v["warnings"] += note_warnings
    feedback = ""
    if v["errors"]:
        feedback = "Fix these problems in your operations:\n- " + "\n- ".join(v["errors"])
    return {"validation": v, "operations": v["operations"], "notes": notes,
            "questions": list(state.get("questions") or []) + v["questions"],
            "feedback": feedback,
            "trace": _trace(state, "validate", t0, errors=len(v["errors"]), warnings=len(v["warnings"]))}


def schedule(state):
    t0 = time.perf_counter()
    snap = state["snapshot"]
    draft, _changes, _errs = apply_operations(snap["tasks"], state.get("operations") or [])
    shifts = propagate(draft)
    an = analyze_plan(draft, snap["project"], snap["participants"], _today(state), state.get("aliases"))
    return {"draft_tasks": draft, "shifts": shifts, "analysis": an,
            "trace": _trace(state, "schedule", t0, shifts=len(shifts), incongruences=len(an["incongruences"]))}


def propose(state):
    t0 = time.perf_counter()
    ops = state.get("operations") or []
    snap = state["snapshot"]
    if not ops and not state.get("questions") and not state.get("notes"):
        return {"proposal": None, "trace": _trace(state, "propose", t0, empty=True)}
    counts = {}
    for o in ops:
        counts[o["op"]] = counts.get(o["op"], 0) + 1
    proposal = {
        "proposalId": uuid.uuid4().hex[:12],
        "projectId": snap["project"].get("projectId", ""),
        "basePlanVersion": snap["planVersion"],
        "trigger": state.get("trigger"),
        "createdAt": datetime.utcnow().isoformat(),
        "status": "pending",
        "summary": state.get("summary", ""),
        "counts": counts,
        "operations": ops,
        "questions": state.get("questions") or [],
        "notes": state.get("notes") or [],
        "coverage": state.get("coverage") or [],
        "shifts": state.get("shifts") or [],
        "warnings": (state.get("validation") or {}).get("warnings", []),
        "unresolved_errors": (state.get("validation") or {}).get("errors", []),
        "analysis_after": state.get("analysis") or {},
        "facts": state.get("facts") or [],
        "topics": state.get("topics") or [],
    }
    return {"proposal": proposal, "trace": _trace(state, "propose", t0, counts=counts)}


def apply_decision(state):
    """Approve / reject the open proposal. Pure code."""
    t0 = time.perf_counter()
    intent = state.get("intent") or {}
    prop = state.get("open_proposal")
    if not prop:
        return {"answer": "No hay ninguna propuesta abierta para este proyecto.",
                "trace": _trace(state, "apply", t0, applied=False)}
    if prop.get("basePlanVersion", 0) != state["snapshot"]["planVersion"]:
        return {"answer": "El plan cambió desde que se hizo esta propuesta; hay que recalcularla.",
                "proposal": {**prop, "status": "stale"}, "trace": _trace(state, "apply", t0, stale=True)}
    if intent.get("decision") == "reject_all":
        return {"proposal": {**prop, "status": "rejected"}, "answer": "Propuesta rechazada. No se cambió nada.",
                "trace": _trace(state, "apply", t0, rejected=True)}
    approved = None if intent.get("decision") == "approve_all" else (intent.get("op_indexes") or [])
    new_export, events = apply_proposal(state["export"], prop, approved, actor="leader")
    status = "applied" if approved is None or len(approved) == len(prop.get("operations") or []) else "partially_applied"
    return {"result_export": new_export, "proposal": {**prop, "status": status, "approved": approved},
            "answer": f"Aplicado: {len([e for e in events if e['op'] not in ('shift', 'notes')])} cambios"
                      f"{' y ' + str(len([e for e in events if e['op'] == 'shift'])) + ' fechas movidas por dependencias' if any(e['op']=='shift' for e in events) else ''}.",
            "trace": _trace(state, "apply", t0, events=len(events), status=status)}


# ── LLM nodes ───────────────────────────────────────────────────────────────

def interpret_request(state, llm):
    t0 = time.perf_counter()
    snap = state["snapshot"]
    hist = "\n".join(f"{m.get('role')}: {m.get('content','')[:400]}" for m in (state.get("history") or [])[-6:])
    prop = state.get("open_proposal")
    prop_txt = (f"{len(prop.get('operations') or [])} operations, {len(prop.get('questions') or [])} questions: "
                + "; ".join(f"Q{i}: {q.get('text','')}" for i, q in enumerate(prop.get("questions") or [], 1))) if prop else "none"
    system = _fill(prompts.INTERPRET, project=_project_line(snap),
                   documents=", ".join(s["name"] for s in snap["sources"]) or "none",
                   proposal=prop_txt, history=hist or "(empty)", message=state.get("message", ""))
    out, err = call_structured(llm, Intent, system, is_empty=lambda o: not o.instruction)
    intent = _dump(out)
    if intent.get("decision") not in ("approve_all", "reject_all", "approve_some", "answer_question"):
        intent["decision"] = ""
    if err:
        intent = {"kind": "question", "instruction": state.get("message", ""), "language": "es",
                  "decision": "", "op_indexes": [], "document": ""}
    upd = {"intent": intent, "trace": _trace(state, "interpret_request", t0, intent=intent["kind"], error=err)}
    if intent["kind"] in ("change", "feedback") or intent.get("decision") == "answer_question":
        upd["instruction"] = intent["instruction"]
        if intent.get("decision") == "answer_question" and prop:
            upd["instruction"] += "\nPending questions were: " + "; ".join(q.get("text", "") for q in prop.get("questions") or [])
    return upd


def extract_facts(state, llm):
    """One call per chunk, in parallel. Chunks are independent on purpose:
    'pending on day 12' in one and 'done on day 17' in another both reach
    reconcile with their dates, and reconcile applies "later wins"."""
    t0 = time.perf_counter()
    snap = state["snapshot"]
    material = list(state.get("material") or [])
    if not material and state.get("trigger") == "chat":
        material = [{"name": "chat", "text": state.get("message", ""), "meta": {"kind": "message"}}]
    known = sorted(set(p["name"] for p in snap["participants"]))
    jobs = []
    for m in material:
        parts = [c for c in _chunks(m["text"]) if c.strip()]
        for k, chunk in enumerate(parts):
            label = m["name"] + (f" (parte {k + 1}/{len(parts)})" if len(parts) > 1 else "")
            jobs.append((m["name"], label, chunk))

    def run(job):
        name, label, chunk = job
        system = _fill(prompts.EXTRACT, today=state.get("today", ""), project=_project_line(snap),
                       people=", ".join(known) or "none", material=f"[source: {label}]\n{chunk}")
        out, err = call_structured(llm, FactsOutput, system, is_empty=lambda o: not o.facts)
        return name, label, out, err

    facts, people, errors = [], set(known), []
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        for name, label, out, err in pool.map(run, jobs):
            if err and err != "empty output":
                errors.append(f"{label}: {err}")
            elif err:
                errors.append(f"{label}: sin hechos")
            for f in out.facts:
                d = _dump(f)
                for e in d.get("evidence") or []:
                    e["source"] = e.get("source") or name
                facts.append(d)
            people.update(out.people)
    facts.sort(key=lambda f: f.get("date") or "9999")
    return {"facts": facts, "people": sorted(people),
            "trace": _trace(state, "extract_facts", t0, calls=len(jobs), facts=len(facts), errors=errors)}


# ── consolidate: facts -> topics with their state at the end ────────────────
CONSOLIDATE_FROM = 20      # fewer facts than this: one topic per fact, no LLM call
_KIND_STATE = {"done": "done", "unblocked": "done", "blocked": "blocked", "in_progress": "in_progress",
               "new_work": "open", "request_to_client": "open", "date_commitment": "open", "risk": "open",
               "decision": "info", "person": "info"}
TOPIC_STATES = ("open", "in_progress", "blocked", "done", "dropped", "info")


def _topic_from_fact(i, f) -> dict:
    due = f.get("date") if f.get("kind") == "date_commitment" else ""
    return {"topic": (f.get("text") or "")[:80], "state": _KIND_STATE.get(f.get("kind"), "info"),
            "summary": f.get("text") or "", "owner": f.get("who") or "", "due_date": due or "",
            "date_status": "committed" if due else "", "last_date": f.get("date") or "",
            "facts": [i], "evidence": list(f.get("evidence") or [])[:1]}


def consolidate(state, llm):
    """Many facts -> few topics, each with its state at the END of the material.
    The plan is decided on topics: 'Twilio: number bought (5/3), WhatsApp
    connected (7/4)' instead of five dated facts the next step must reconcile.
    Facts no topic claims are kept as their own topic, so nothing is lost."""
    t0 = time.perf_counter()
    facts = state.get("facts") or []
    if len(facts) < CONSOLIDATE_FROM:
        topics = [_topic_from_fact(i, f) for i, f in enumerate(facts, 1)]
        return {"topics": topics, "trace": _trace(state, "consolidate", t0, topics=len(topics), llm=False)}
    snap = state["snapshot"]
    system = _fill(prompts.CONSOLIDATE, today=state.get("today", ""), project=_project_line(snap),
                   people=", ".join(state.get("people") or []) or "none", facts=_facts_block(facts))
    out, err = call_structured(llm, TopicsOutput, system, is_empty=lambda o: not o.topics)
    topics, claimed = [], set()
    for t in out.topics:
        d = _dump(t)
        d["state"] = d.get("state") if d.get("state") in TOPIC_STATES else "open"
        d["date_status"] = "committed" if d.get("due_date") and d.get("date_status") == "committed" else (
            "estimated" if d.get("due_date") else "")
        d["facts"] = [n for n in d.get("facts") or [] if 1 <= n <= len(facts)]
        if not d.get("evidence"):
            ev = [e for n in d["facts"][-1:] for e in facts[n - 1].get("evidence") or []][:1]
            d["evidence"] = ev
        claimed.update(d["facts"])
        topics.append(d)
    orphans = [i for i in range(1, len(facts) + 1) if i not in claimed and facts[i - 1].get("kind") != "person"]
    if out.topics:
        # Kept as context only: a fact the consolidation left out is usually an
        # old status line ("estoy trabajando en ...") that would become a stale task.
        topics += [dict(_topic_from_fact(i, facts[i - 1]), state="info") for i in orphans]
    else:
        topics += [_topic_from_fact(i, facts[i - 1]) for i in orphans]   # consolidation failed: keep everything
    topics.sort(key=lambda t: t.get("last_date") or "9999")
    return {"topics": topics,
            "trace": _trace(state, "consolidate", t0, facts=len(facts), topics=len(topics), orphans=len(orphans),
                            states={s: sum(1 for t in topics if t["state"] == s) for s in TOPIC_STATES}, error=err)}


def _topics_block(topics) -> str:
    lines = []
    for i, t in enumerate(topics or [], 1):
        ev = "; ".join(f"{e.get('source', '')} {e.get('locator', '')} '{e.get('quote', '')}'"
                       for e in t.get("evidence") or [])
        due = f"; due: {t['due_date']} [{t.get('date_status') or 'estimated'}]" if t.get("due_date") else ""
        lines.append(f"T{i}. [{t.get('state')}] {t.get('topic')}: {t.get('summary')} "
                     f"(owner: {t.get('owner') or '-'}; last: {t.get('last_date') or '-'}{due}; evidence: {ev})")
    return "\n".join(lines) or "(none)"


def _material_block(state) -> tuple:
    """(text, count) of what reconcile reasons on: topics when there are, facts otherwise."""
    if state.get("topics") is not None:
        return _topics_block(state["topics"]), len(state["topics"])
    return _facts_block(state.get("facts")), len(state.get("facts") or [])


def _facts_block(facts) -> str:
    def _ev(f):
        return "; ".join(f"{e.get('source', '')} {e.get('locator', '')} '{e.get('quote', '')}'"
                         for e in f.get("evidence") or [])
    return "\n".join(
        f"F{i}. [{f.get('kind')}] {f.get('text')} (who: {f.get('who') or '-'}; "
        f"date: {f.get('date') or '-'}; evidence: {_ev(f)})"
        for i, f in enumerate(facts or [], 1)) or "(none)"


def _all_tasks_index(tasks) -> str:
    return "\n".join(f"{t['taskId']} | {t.get('status', '')} | {(t.get('text') or '')[:110]}" for t in tasks)


SCOPE_BATCH = ("Return operations ONLY for the tasks listed under CURRENT TASKS (one batch of the plan). "
               "Do NOT create tasks and do NOT ask whether to create them: the tasks marked NEW in ALL TASKS "
               "are already proposed. An existing task that a NEW task replaces (same work, more specific) "
               "is deleted with duplicate_of = that new id. For duplicates you may point duplicate_of at any "
               "task in ALL TASKS.")
MAX_NOTES = 15
COVER_KINDS = ("email", "meeting", "call", "decision", "idea", "info")


def _existing_notes(snap) -> str:
    rows = [i for i in snap.get("insights") or [] if i.get("type") == "note"]
    return "\n".join(f"- {r.get('eventDate') or '-'} {r.get('title', '')[:140]}" for r in rows[-40:]) or "(none)"


def _coverage_gaps(out, n_facts: int) -> list:
    """What the cover call left undecided: facts without an entry, and
    'create' entries that point to no create."""
    seen = {c.fact for c in out.coverage}
    new_ids = {(o.task_id or "").strip() for o in out.creates}
    gaps = [f"T{i}: no coverage entry" for i in range(1, n_facts + 1) if i not in seen]
    gaps += [f"T{c.fact}: action 'create' but no create with id '{c.task_id}'"
             for c in out.coverage if c.action == "create" and (c.task_id or "").strip() not in new_ids]
    return gaps


def _cover(state, llm, index, facts_txt, n_facts):
    """Fact by fact: covered / create / note / ignore. One retry when facts
    are left undecided, so 'everything is covered' cannot be said in bulk."""
    snap = state["snapshot"]
    feedback = state.get("feedback") or "none"
    out, err, gaps = CoverOutput(), "", []
    for _attempt in range(2):
        system = _fill(prompts.COVER, today=state.get("today", ""), project=_project_line(snap),
                       participants=_participants_line(snap), people=", ".join(state.get("people") or []) or "none",
                       all_tasks=index, existing_notes=_existing_notes(snap), facts=facts_txt,
                       instruction=state.get("instruction") or "none", feedback=feedback)
        out, err = call_structured(llm, CoverOutput, system,
                                   is_empty=lambda o: not o.coverage and not o.creates and not o.notes)
        gaps = _coverage_gaps(out, n_facts) if not err else []
        if err or not gaps:
            break
        feedback = "You left these topics undecided; return the full answer again:\n- " + "\n- ".join(gaps[:30])
    return out, err, gaps


_STOP = set("a an the of to for and or in on with de la el los las del y o en para con por un una que se su sus".split())


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", norm(text or "")) if len(w) > 2 and w not in _STOP}


def _similar_first(tasks):
    """Order tasks so near-duplicates sit next to each other (and so in the
    same batch, where one call can see both and keep one)."""
    rest, out = list(tasks), []
    while rest:
        seed = rest.pop(0)
        out.append(seed)
        ws = _words(seed.get("text"))
        close = [t for t in rest if ws and len(ws & _words(t.get("text"))) / len(ws | _words(t.get("text")) or {1}) >= 0.5]
        for t in close:
            rest.remove(t)
            out.append(t)
    return out


def _review_gaps(out, batch) -> list:
    """Tasks of the batch without a review, and reviews whose verdict does not
    match the operations returned for that task."""
    ids = [t["taskId"] for t in batch]
    by_task = {}
    for o in out.operations:
        by_task.setdefault(o.task_id, set()).add(o.op)
    asked = {tid for q in out.questions for tid in q.about_task_ids}
    reviewed = {r.task_id: r for r in out.reviews}
    gaps = [f"task {tid}: no review" for tid in ids if tid not in reviewed]
    for tid, r in reviewed.items():
        if tid not in ids:
            continue
        v, got = (r.verdict or "").strip(), by_task.get(tid, set()) - {"link"}
        if v == "keep" and got:
            gaps.append(f"task {tid}: verdict keep but you returned {sorted(got)}")
        elif v == "question" and tid not in asked:
            gaps.append(f"task {tid}: verdict question but no question mentions it")
        elif v in ("update", "close", "delete", "block", "reopen") and v not in got:
            gaps.append(f"task {tid}: verdict {v} but no '{v}' operation on it")
    return gaps


def _close_instead_of_delete(tasks, ops):
    """A 'duplicate' of DONE work is finished work: close it (history is kept)
    instead of deleting it. Same when the kept task is being closed now."""
    status = {t["taskId"]: t.get("status") for t in tasks}
    closing = {o.get("task_id") for o in ops if o.get("op") == "close"}
    for o in ops:
        dup = (o.get("duplicate_of") or "").strip()
        if o.get("op") == "delete" and dup and (status.get(dup) == "done" or dup in closing) \
                and status.get(o.get("task_id")) != "done":
            o["op"] = "close"
            o["reason"] = (o.get("reason") or "") + " (cerrada: el trabajo ya está hecho en " + dup[:8] + ")"
            o["duplicate_of"] = ""
    return ops


STALE_DAYS = 30
SAME_TASK = 0.6     # word overlap (Jaccard) above which two new tasks are the same one


def _stale_topics(topics) -> set:
    """Topic numbers (1-based) still open with no news in the last STALE_DAYS
    of the material and no committed deadline: they are asked about, not
    created. Decided by code because the model applied the rule unevenly."""
    dates = [parse_date(t.get("last_date")) for t in topics or []]
    known = [d for d in dates if d]
    if not known:
        return set()
    latest = max(known)
    return {i for i, (t, d) in enumerate(zip(topics, dates), 1)
            if t.get("state") in ("open", "in_progress", "blocked") and not t.get("due_date")
            and d and (latest - d).days > STALE_DAYS}


def _merge_same_creates(creates):
    """Two new tasks with almost the same words are one: keep the first,
    return (kept, {dropped_id: kept_id})."""
    kept, merged = [], {}
    for c in creates:
        w = _words(c.get("text"))
        twin = next((k for k in kept if w and len(w & _words(k.get("text"))) / len(w | _words(k.get("text"))) >= SAME_TASK),
                    None)
        if twin:
            merged[c["task_id"]] = twin["task_id"]
        else:
            kept.append(c)
    return kept, merged


def reconcile(state, llm):
    """Small calls instead of one big one: first one 'cover' call walks the
    topics one by one and creates what is missing (or records it as a note);
    then the existing tasks are judged in batches of TASKS_PER_BATCH, seeing
    a one-line index of ALL tasks plus the new ones, so duplicates across
    batches and against new work are visible."""
    t0 = time.perf_counter()
    snap = state["snapshot"]
    tasks = snap["tasks"]
    stale = _stale_topics(state.get("topics")) if state.get("topics") is not None else set()
    if stale:
        view = [dict(t, state="stale") if i in stale else t for i, t in enumerate(state["topics"], 1)]
        facts_txt, n_items = _topics_block(view), len(view)
    else:
        facts_txt, n_items = _material_block(state)
    index = _all_tasks_index(tasks)

    def judge(batch, all_index):
        """One batch. Every task needs a review whose verdict matches the
        operations; one retry when tasks are skipped or verdicts disagree."""
        feedback = state.get("feedback") or "none"
        out, err, gaps = ReconcileOutput(), "", []
        for _attempt in range(2):
            system = _fill(prompts.RECONCILE, today=state.get("today", ""), project=_project_line(snap),
                           participants=_participants_line(snap),
                           people=", ".join(state.get("people") or []) or "none",
                           scope=SCOPE_BATCH, all_tasks=all_index, tasks=_tasks_block(batch), facts=facts_txt,
                           instruction=state.get("instruction") or "none",
                           incongruences=json.dumps(state.get("incongruences_in") or [], ensure_ascii=False) or "none",
                           feedback=feedback)
            out, err = call_structured(llm, ReconcileOutput, system,
                                       is_empty=lambda o: not o.reviews and not o.operations and not o.questions)
            gaps = _review_gaps(out, batch) if not err else []
            if err or not gaps:
                break
            feedback = "Your answer was incomplete; return the full answer again fixing:\n- " + "\n- ".join(gaps[:30])
        return out, err, gaps

    # 1. Cover first: the new work the material asks for, decided topic by topic
    #    against the CURRENT tasks.
    cover_out, cover_err, cgaps = CoverOutput(), "", []
    if n_items:
        cover_out, cover_err, cgaps = _cover(state, llm, index, facts_txt, n_items)
    mapping, creates = {}, []
    for o in cover_out.creates:
        d = _dump(o)
        d["op"] = "create"
        new_id = f"new-{len(creates) + 1}"
        old = (d.get("task_id") or "").strip()
        if old and old not in mapping:
            mapping[old] = new_id
        d["task_id"] = new_id
        creates.append(d)
    # Code-enforced: nothing is created from a stale topic, and near-identical
    # new tasks are merged into one.
    from_stale = {mapping.get(c.task_id, c.task_id) for c in cover_out.coverage
                  if c.action == "create" and c.fact in stale}
    creates = [d for d in creates if d["task_id"] not in from_stale]
    creates, merged = _merge_same_creates(creates)
    for old_id, new_id in list(mapping.items()):
        mapping[old_id] = merged.get(new_id, new_id)
    for d in creates:
        d["depends_on"] = sorted({merged.get(mapping.get(x, x), mapping.get(x, x)) for x in d.get("depends_on") or []}
                                 - {d["task_id"]} - from_stale)

    # 2. Then the existing tasks, in batches, KNOWING the new ones: a generic
    #    task replaced by specific new deliverables can be deleted/updated
    #    pointing at them instead of staying as a duplicate.
    nonlocal_index = index + ("\n" + "\n".join(f"{d['task_id']} | NEW (proposed) | {(d.get('text') or '')[:110]}"
                                               for d in creates) if creates else "")
    ordered = _similar_first(tasks)
    batches = [ordered[i:i + TASKS_PER_BATCH] for i in range(0, len(ordered), TASKS_PER_BATCH)]
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        results = list(pool.map(lambda b: judge(b, nonlocal_index), batches))

    ops, questions, summaries, errors, per_call, reviews = [], [], [], [], [], []
    for batch, (out, err, gaps) in zip(batches, results):
        allowed = {t["taskId"] for t in batch}
        verdicts = {}
        for r in out.reviews:
            verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
        per_call.append({"scope": "batch", "tasks": len(batch), "ops_raw": [o.op for o in out.operations],
                         "verdicts": verdicts, "questions": len(out.questions), "gaps": gaps[:10],
                         "summary": (out.summary or "")[:200], "error": err})
        if err:
            errors.append(err)
        ops += [_dump(o) for o in out.operations if o.op != "create" and o.task_id in allowed]
        reviews += [_dump(r) for r in out.reviews if r.task_id in allowed]
        questions += [_dump(q) for q in out.questions]
        if out.summary:
            summaries.append(out.summary)
    ops = _close_instead_of_delete(tasks, ops)
    done = {t["taskId"] for t in tasks if t.get("status") == "done"}
    ops = [o for o in ops if not (o.get("op") == "close" and o.get("task_id") in done)]   # no-op

    notes = []
    for n in cover_out.notes:
        d = _dump(n)
        d["task_id"] = mapping.get((d.get("task_id") or "").strip(), (d.get("task_id") or "").strip())
        d["kind"] = d.get("kind") if d.get("kind") in COVER_KINDS else "info"
        notes.append(d)
    coverage = [{"fact": c.fact, "action": c.action, "task_id": mapping.get(c.task_id, c.task_id)}
                for c in cover_out.coverage]
    ops += creates
    if stale:
        names = [state["topics"][i - 1].get("topic", "") for i in sorted(stale)]
        questions.insert(0, {"id": "stale", "text": "¿Siguen abiertos estos temas sin noticias recientes? "
                             + "; ".join(n[:60] for n in names[:8]) + (" …" if len(names) > 8 else ""),
                             "about_task_ids": [], "options": ["Sí, crear tareas", "No, están cerrados o descartados"]})
    questions += [_dump(q) for q in cover_out.questions[:3]]
    if cover_out.summary:
        summaries.append(cover_out.summary)
    if cover_err:
        errors.append(cover_err)
    acts = {}
    for c in coverage:
        acts[c["action"]] = acts.get(c["action"], 0) + 1
    per_call.append({"scope": "cover", "items": n_items, "coverage": acts,
                     "creates": len(creates), "notes": len(notes), "gaps": cgaps[:10],
                     "stale_topics": len(stale), "dropped_from_stale": len(from_stale), "merged": len(merged),
                     "summary": (cover_out.summary or "")[:200], "error": cover_err})

    for i, q in enumerate(questions, 1):
        q["id"] = q.get("id") or f"q{i}"
    return {"operations": ops, "questions": questions[:5], "notes": notes[:MAX_NOTES], "coverage": coverage,
            "summary": " ".join(summaries)[:600],
            "iteration": (state.get("iteration") or 0) + 1, "feedback": "",
            "reviews": reviews,
            "trace": _trace(state, "reconcile", t0, calls=len(batches) + (1 if n_items else 0), ops=len(ops),
                            questions=len(questions), notes=len(notes),
                            round=(state.get("iteration") or 0) + 1, errors=errors, per_call=per_call)}


def link_dependencies(state, llm):
    t0 = time.perf_counter()
    snap = state["snapshot"]
    draft, _c, _e = apply_operations(snap["tasks"], state.get("operations") or [])
    open_tasks = [t for t in draft if t.get("status") in ("pending", "in_progress", "blocked")]
    created = [o for o in state.get("operations") or [] if o.get("op") == "create"]
    # apply_operations renumbers creates when their id clashes; map by order.
    new_ids = [t["taskId"] for t in draft if t.get("createdBy") == "planning"]
    focus = new_ids + [o["task_id"] for o in state.get("operations") or []
                       if o.get("op") == "update" and o.get("task_id")]
    if not focus:
        return {"trace": _trace(state, "link_dependencies", t0, skipped=True)}
    system = _fill(prompts.LINK, tasks="\n".join(f"{t['taskId']} | {t.get('status')} | {t.get('text','')[:200]}"
                                                 for t in open_tasks), focus=", ".join(focus))
    out, err = call_structured(llm, LinkOutput, system, is_empty=lambda o: False, retries=1)
    ops = list(state.get("operations") or [])
    for i, c in enumerate(created):
        if i < len(new_ids):
            c["task_id"] = new_ids[i]
    links = [_dump(link) for link in out.links if link.op == "link" and link.depends_on]
    for link in links:
        link["op"] = "link"
        # A link on a task being created goes into that create, so the
        # proposal shows one operation per task.
        target = next((o for o in ops if o.get("op") == "create" and o.get("task_id") == link["task_id"]), None)
        if target:
            target["depends_on"] = sorted(set(target.get("depends_on") or []) | set(link["depends_on"]))
        else:
            link.setdefault("evidence", [])
            ops.append(link)
    return {"operations": ops, "trace": _trace(state, "link_dependencies", t0, links=len(links), error=err)}


def answer(state, llm):
    t0 = time.perf_counter()
    snap = state["snapshot"]
    intent = state.get("intent") or {}
    system = _fill(prompts.ANSWER, language=intent.get("language") or "es",
                   question=intent.get("instruction") or state.get("message", ""),
                   tasks=_tasks_block(snap["tasks"]),
                   analysis=json.dumps(state.get("analysis") or {}, ensure_ascii=False)[:20000])
    out, err = call_structured(llm, TextOutput, system, is_empty=lambda o: not o.text.strip())
    return {"answer": out.text or "No pude responder ahora.", "trace": _trace(state, "answer", t0, error=err)}


def narrate(state, llm):
    t0 = time.perf_counter()
    prop = state.get("proposal")
    intent = state.get("intent") or {}
    if not prop:
        return {"narration": state.get("answer") or "No encontré cambios que proponer.",
                "trace": _trace(state, "narrate", t0, empty=True)}
    by_id = {t["taskId"]: t for t in state["snapshot"]["tasks"]}
    ops_txt = "\n".join(
        f"{i}. {o['op']} {o.get('task_id','')} '{(o.get('text') or by_id.get(o.get('task_id'), {}).get('text',''))[:90]}' — {o.get('reason','')[:120]}"
        for i, o in enumerate(prop["operations"], 1))
    system = _fill(prompts.NARRATE, language=intent.get("language") or "es",
                   what=state.get("summary") or state.get("instruction") or state.get("trigger", ""),
                   operations=ops_txt or "(none)",
                   questions="\n".join(f"{i}. {q.get('text','')}" for i, q in enumerate(prop["questions"], 1)) or "(none)",
                   shifts=json.dumps(prop.get("shifts") or [], ensure_ascii=False)[:2000],
                   warnings="; ".join(prop.get("warnings") or []) or "none")
    out, err = call_structured(llm, TextOutput, system, is_empty=lambda o: not o.text.strip())
    return {"narration": out.text or prop.get("summary", ""), "trace": _trace(state, "narrate", t0, error=err)}
