"""Consolidation: what the members told -> ONE proposal + a digest for the owner.

    member_updates (classified items, nothing changed)  ─┐
    Trello board (cards linked to tasks, if connected)  ─┴─> build() ─> proposal (pending)
                                                                    └─> digest in the planning chat
                                                                        + notification to the owner

build() is deterministic on purpose. The judgement (what the member meant,
how sure, which task) was made by the AI in the member chat, item by item,
with the member there to answer. Here only GUARD RULES apply, and they must
be predictable for the owner to trust the digest:

  * nothing is closed on hearsay or on low certainty;
  * two people contradicting each other on a task -> no change, a question;
  * a conditional date ("el jueves si Rafa me pasa las urls") does not move
    the due date: it becomes a dependency and a note;
  * ideas only the owner can decide are questions, never operations;
  * the assignee's own word wins over anyone else's.

Nothing is applied here: the owner approves in Intelligence > Replanificación,
and the approval is reflected in the app and in Trello (trello_reflect.py).
"""
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import List, Optional

from agent.planning.sources import match_person, normalize_participant, norm
from api.services import checkin, member_updates, planning_chat

DONE_LIST_WORDS = ("hecho", "done", "terminad", "completad", "finaliz", "cerrad", "listo")
DIGEST_HOUR_UTC = int(os.environ.get("UPDATES_DIGEST_HOUR_UTC", "22"))
STATUS_TYPES = ("done", "progress", "not_started", "blocker")
TODAY = ""          # tests pin the date


def _now() -> datetime:
    return datetime.fromisoformat(TODAY + "T12:00:00") if TODAY else datetime.utcnow()


def _label(t: Optional[dict], n: int = 70) -> str:
    return ((t or {}).get("text") or "").strip()[:n] or "(sin tarea)"


def _ev(person: str, day: str, quote: str) -> list:
    return [{"source": f"update {person}", "locator": day, "quote": (quote or "")[:120]}]


def _assignee_key(task: dict, parts: list) -> str:
    who = (task.get("assignedTo") or "").strip()
    if not who:
        return ""
    p = next((q for q in parts if q.get("email") and q["email"] == who.lower()), None) or match_person(who, parts)
    return checkin.person_key(p) if p else norm(who)


# ── Trello: what the board says about linked tasks ──────────────────────────

def trello_signals(uid: str, proj: dict, tasks: dict) -> dict:
    """{signals: [{task_id, kind: done|due, due, list, card}], unlinked: n, error}"""
    from api.services import trello as trello_service
    out: dict = {"signals": [], "unlinked": 0, "error": ""}
    board = proj.get("trelloBoardId")
    if not board:
        return out
    by_card = {t["trelloCardId"]: t for t in tasks.values() if t.get("trelloCardId")}
    try:
        for lst in trello_service.list_board_lists(uid, board):
            is_done = any(w in norm(lst["name"]) for w in DONE_LIST_WORDS)
            for c in trello_service.list_cards(uid, lst["listId"]):
                if c.get("closed"):
                    continue
                t = by_card.get(c["cardId"])
                if not t:
                    out["unlinked"] += 1
                    continue
                if is_done and t.get("status") != "done":
                    out["signals"].append({"task_id": t["taskId"], "kind": "done", "list": lst["name"],
                                           "card": c["name"]})
                elif c.get("due") and c["due"] != (t.get("dueDate") or "") and t.get("status") != "done":
                    out["signals"].append({"task_id": t["taskId"], "kind": "due", "due": c["due"],
                                           "list": lst["name"], "card": c["name"]})
    except Exception as e:
        out["error"] = str(getattr(e, "detail", e))[:200]
    return out


# ── the pure part ───────────────────────────────────────────────────────────

def build(proj: dict, tasks: dict, sessions: List[dict], trello: Optional[dict] = None, today: str = "") -> dict:
    """sessions: closed member sessions with parsed items. Returns the proposal
    pieces plus what the digest needs. No I/O."""
    today = today or _now().date().isoformat()
    parts = [normalize_participant(p) for p in proj.get("participants") or []]
    owner_name = member_updates.owner_name(proj) if proj.get("userId") else ""
    ops, notes, questions = [], [], []
    urgent, decisions, contradictions, done_lines, other_lines = [], [], [], [], []
    reported = []
    claims = defaultdict(list)                 # task_id -> [(person, key, item)]
    for s in sessions:
        person, key = s.get("person") or s.get("email") or "?", s.get("personKey", "")
        if person not in reported:
            reported.append(person)
        for it in s.get("items") or []:
            it = {**it, "_person": person, "_key": key}
            tid = it.get("task_id") or ""
            if tid and tid not in tasks:
                tid = it["task_id"] = ""
            typ = it.get("type") or ""
            if it.get("urgent"):
                urgent.append(it)
            if tid and (typ in STATUS_TYPES or typ == "hearsay"):
                claims[tid].append((person, key, it))
            ev = _ev(person, today, it.get("quote") or it.get("text"))
            if typ in ("idea", "question") or it.get("decision_for_owner"):
                if it.get("decision_for_owner") or typ == "question":
                    decisions.append(it)
                    questions.append({"id": f"u{len(questions) + 1}", "text": f"{person}: {it.get('text', '')}",
                                      "about_task_ids": [tid] if tid else [], "options": []})
                notes.append({"kind": "idea" if typ == "idea" else "info", "text": f"{person}: {it.get('text', '')}"[:300],
                              "who": person, "date": today, "task_id": tid, "evidence": ev})
                continue
            if typ == "hearsay":
                notes.append({"kind": "info", "text": f"Según {person}: {it.get('text', '')}"[:300], "who": person,
                              "date": today, "task_id": tid, "evidence": ev})
                continue
            if typ == "plan" and not tid:
                if (it.get("certainty") or "sure") in ("sure", "estimated"):
                    ops.append({"op": "create", "task_id": f"new-{len(ops) + 1}", "text": it.get("text", "")[:200],
                                "assigned_to": it.get("owner") or person, "due_date": it.get("due_date") or "",
                                "date_status": "committed" if it.get("certainty") == "sure" and it.get("due_date") else
                                ("estimated" if it.get("due_date") else ""),
                                "reason": f"Plan contado por {person}", "evidence": ev, "confidence": "medium"})
                else:
                    notes.append({"kind": "info", "text": f"{person} (plan, {it.get('certainty')}): {it.get('text', '')}"[:300],
                                  "who": person, "date": today, "task_id": "", "evidence": ev})
                continue
            if typ == "blocker" and not tid:
                notes.append({"kind": "info", "text": f"Bloqueo de {person}: {it.get('text', '')}"[:300], "who": person,
                              "date": today, "task_id": "", "evidence": ev})
                continue
            if not tid:
                continue
            t = tasks[tid]
            # dates and dependencies
            if typ in ("date_change", "plan") and it.get("due_date") and t.get("status") != "done":
                cert = it.get("certainty") or "estimated"
                if cert in ("sure", "estimated") and it["due_date"] != (t.get("dueDate") or ""):
                    ops.append({"op": "update", "task_id": tid, "due_date": it["due_date"],
                                "date_status": "committed" if cert == "sure" else "estimated",
                                "reason": f"{person}: nueva fecha {it['due_date']}" + (" (estimada)" if cert != "sure" else ""),
                                "evidence": ev, "confidence": "high" if cert == "sure" else "medium"})
                    other_lines.append(f"{_label(t)}: nueva fecha {it['due_date']}"
                                       + (" (estimada)" if cert != "sure" else "") + f" ({person})")
                else:
                    notes.append({"kind": "info", "text": f"{person}: {it.get('text', '')} (fecha {it['due_date']}, "
                                  f"{cert})"[:300], "who": person, "date": today, "task_id": tid, "evidence": ev})
                    other_lines.append(f"{_label(t)}: {it['due_date']} solo si se cumple una condición ({person})")
            if it.get("depends_on") and it["depends_on"] in tasks and it["depends_on"] != tid and \
                    it["depends_on"] not in (t.get("dependsOn") or []) and \
                    not any(o["op"] == "link" and o["task_id"] == tid and it["depends_on"] in o["depends_on"] for o in ops):
                ops.append({"op": "link", "task_id": tid, "depends_on": [it["depends_on"]],
                            "reason": f"{person}: depende de «{_label(tasks[it['depends_on']], 50)}»",
                            "evidence": ev, "confidence": "medium"})

    # status per task: contradictions first, then the assignee's word
    for tid, cl in claims.items():
        t = tasks[tid]
        a_key = _assignee_key(t, parts)
        pos = [(p, k, i) for p, k, i in cl if i["type"] in ("done", "hearsay")]
        neg = [(p, k, i) for p, k, i in cl if i["type"] in ("progress", "not_started", "blocker")]
        if pos and neg and {k for _p, k, _i in pos} != {k for _p, k, _i in neg}:
            said = "; ".join(f"{p} dice «{(i.get('quote') or i.get('text') or '')[:80]}»" for p, _k, i in pos + neg)
            contradictions.append({"task_id": tid, "task": _label(t), "text": said})
            questions.append({"id": f"u{len(questions) + 1}", "text": f"¿Cómo está «{_label(t, 60)}»? {said}",
                              "about_task_ids": [tid], "options": ["Terminada", "En progreso", "Bloqueada"]})
            # a blocker still counts: the assignee says it cannot move
        status_items = [x for x in cl if x[2]["type"] in STATUS_TYPES]
        own = [x for x in status_items if x[1] == a_key] or status_items
        if not own:
            continue
        person, _k, it = own[-1]
        if (it.get("certainty") or "sure") == "low":
            continue
        ev = _ev(person, today, it.get("quote") or it.get("text"))
        contested = any(c["task_id"] == tid for c in contradictions)
        if it["type"] == "done" and t.get("status") != "done" and not contested:
            ops.append({"op": "close", "task_id": tid, "reason": f"{person}: hecha", "evidence": ev, "confidence": "high"})
            done_lines.append(f"{_label(t)} ({person})")
        elif it["type"] == "blocker":
            why = it.get("text", "")[:200] + (f" (necesita: {it['owner']})" if it.get("owner") else "")
            if t.get("status") != "blocked" or (t.get("blockedReason") or "") != why:
                ops.append({"op": "block", "task_id": tid, "blocked_reason": why, "reason": f"{person}: bloqueada",
                            "evidence": ev, "confidence": "high"})
            other_lines.append(f"{_label(t)}: bloqueada — {why} ({person})")
        elif it["type"] == "progress" and t.get("status") in ("pending", "blocked"):
            ops.append({"op": "update", "task_id": tid, "status": "in_progress", "reason": f"{person}: en curso",
                        "evidence": ev, "confidence": "high"})
            other_lines.append(f"{_label(t)}: en curso ({person})")
        elif it["type"] == "progress":
            other_lines.append(f"{_label(t)}: avanza — {it.get('text', '')[:80]} ({person})")
        elif it["type"] == "not_started":
            other_lines.append(f"{_label(t)}: sin empezar ({person})")

    # Trello, only for linked cards and never over a member's word
    tr = trello or {}
    spoken = {it.get("task_id") for s in sessions for it in s.get("items") or [] if it.get("task_id")}
    for sig in tr.get("signals") or []:
        tid = sig["task_id"]
        if tid not in tasks or tid in spoken:
            continue
        ev = [{"source": "Trello", "locator": sig.get("list", ""), "quote": sig.get("card", "")[:120]}]
        if sig["kind"] == "done" and not any(o["op"] == "close" and o["task_id"] == tid for o in ops):
            ops.append({"op": "close", "task_id": tid, "reason": f"Trello: tarjeta en «{sig.get('list')}»",
                        "evidence": ev, "confidence": "medium"})
            done_lines.append(f"{_label(tasks[tid])} (Trello)")
        elif sig["kind"] == "due" and not any(o["op"] == "update" and o["task_id"] == tid and o.get("due_date") for o in ops):
            ops.append({"op": "update", "task_id": tid, "due_date": sig["due"], "date_status": "committed",
                        "reason": f"Trello: fecha {sig['due']}", "evidence": ev, "confidence": "medium"})
            other_lines.append(f"{_label(tasks[tid])}: fecha {sig['due']} (Trello)")

    # who owes an update and did not give one
    rep_keys = {s.get("personKey") for s in sessions}
    not_reported = []
    for p, _ts in checkin.assignments(proj, list(tasks.values())):
        if checkin.person_key(p) in rep_keys:
            continue
        if owner_name and norm(p.get("name", "")) == norm(owner_name):
            continue
        not_reported.append(p.get("name") or p.get("email"))

    # work nobody is asked about: no assignee, or an assignee who is not a participant
    open_tasks = [t for t in tasks.values() if t.get("status") in checkin.OPEN]
    unassigned = [t for t in open_tasks if norm(t.get("assignedTo") or "") in ("", "sin asignar", "unassigned")]
    strangers = Counter()
    for t in open_tasks:
        a = (t.get("assignedTo") or "").strip()
        if a and t not in unassigned and not (match_person(a, parts) or
                                              next((p for p in parts if p.get("email") == a.lower()), None)):
            strangers[a] += 1

    counts = {}
    for o in ops:
        counts[o["op"]] = counts.get(o["op"], 0) + 1
    return {"unassigned": [_label(t, 60) for t in unassigned], "strangers": dict(strangers),
            "operations": ops, "notes": notes, "questions": questions, "counts": counts, "urgent": urgent,
            "decisions": decisions, "contradictions": contradictions, "reported": reported,
            "not_reported": not_reported, "done_lines": done_lines, "other_lines": other_lines,
            "trello": {"linked_signals": len(tr.get("signals") or []), "unlinked": tr.get("unlinked", 0),
                       "error": tr.get("error", "")}}


def digest_text(proj: dict, tasks: dict, b: dict, today: str) -> str:
    def lab(i):
        t = tasks.get(i.get("task_id") or "")
        return f" — {_label(t, 50)}" if t else ""
    L = [f"Resumen de updates de {proj.get('name', '')} ({today})"]
    L.append("Reportaron: " + (", ".join(b["reported"]) or "nadie")
             + (f". No reportaron: {', '.join(b['not_reported'])}" if b["not_reported"] else ""))
    if b["urgent"]:
        L += ["", "URGENTE:"] + [f"• {i['_person']}: {i.get('text', '')}"
                                  + (f" (necesita: {i['owner']})" if i.get("owner") else "") + lab(i) for i in b["urgent"]]
    if b["contradictions"]:
        L += ["", "No coinciden (no cambié nada, decide tú):"] + [f"• {c['task']}: {c['text']}" for c in b["contradictions"]]
    if b["decisions"]:
        L += ["", "Decisiones para ti:"] + [f"• {i['_person']} propone: {i.get('text', '')}" + lab(i) for i in b["decisions"]]
    if b["done_lines"]:
        L += ["", "Terminado:"] + [f"• {x}" for x in b["done_lines"]]
    if b["other_lines"]:
        L += ["", "Avances y fechas:"] + [f"• {x}" for x in b["other_lines"]]
    if b.get("unassigned") or b.get("strangers"):
        L += ["", "Nadie da update de esto:"]
        if b.get("unassigned"):
            more = f" (y {len(b['unassigned']) - 5} más)" if len(b["unassigned"]) > 5 else ""
            L.append(f"• {len(b['unassigned'])} tareas abiertas sin responsable: "
                     + "; ".join(b["unassigned"][:5]) + more + ". Asígnalas para que alguien reporte.")
        if b.get("strangers"):
            L.append("• Responsables que no están en el proyecto: "
                     + ", ".join(f"{k} ({n})" for k, n in sorted(b["strangers"].items(), key=lambda kv: -kv[1]))
                     + ". Agrégalos como participantes (sin cuenta, tú los actualizas aquí).")
    tr = b.get("trello") or {}
    if tr.get("error"):
        L += ["", f"No pude leer Trello: {tr['error']}"]
    elif tr.get("unlinked"):
        L += ["", f"Trello: {tr['unlinked']} tarjetas no están vinculadas a tareas de OneBox (no las usé)."]
    if b["operations"] or b["questions"]:
        L += ["", planning_chat._describe({"counts": b["counts"], "notes": b["notes"], "questions": b["questions"]})
              + " Revísala y apruébala aquí: al aprobar se refleja en la app" + (" y en Trello." if proj.get("trelloBoardId") else ".")]
    else:
        L += ["", "Sin cambios que proponer en el plan."]
    return "\n".join(L)


# ── I/O ─────────────────────────────────────────────────────────────────────

def _state(project_id: str) -> dict:
    return planning_chat.table().get_item(Key={"projectId": project_id, "itemId": "updstate"}).get("Item") or {}


def consolidate(project_id: str, actor: str = "", use_trello: bool = True, notify: bool = True,
                send_external: bool = True) -> dict:
    """Gather the closed, not yet consolidated member sessions (+ Trello),
    save ONE proposal, post the digest and tell the owner."""
    from agent.tools import projects_table
    from api.services.planning_live import _notify, snapshot
    proj = projects_table.get_item(Key={"projectId": project_id}).get("Item")
    if not proj:
        return {"status": "not_found"}
    sess = member_updates.sessions(project_id)
    export = snapshot(project_id)
    tasks = {t["taskId"]: t for t in export["tasks"]}
    tr = trello_signals(proj.get("userId", ""), proj, tasks) if use_trello and proj.get("trelloBoardId") else {}
    if not sess and not (tr.get("signals")):
        return {"status": "nothing", "sessions": 0}
    now = _now()
    today = now.date().isoformat()
    b = build(proj, tasks, sess, tr, today)
    text = digest_text(proj, tasks, b, today)
    item = None
    if b["operations"] or b["notes"] or b["questions"]:
        proposal = {"proposalId": uuid.uuid4().hex[:12], "projectId": project_id,
                    "basePlanVersion": int((export.get("project") or {}).get("planVersion") or 0),
                    "trigger": "updates", "createdAt": now.isoformat(),
                    "summary": f"Updates del equipo ({today}): " + ", ".join(b["reported"]),
                    "counts": b["counts"], "operations": b["operations"], "questions": b["questions"],
                    "notes": b["notes"], "shifts": [], "warnings": [], "unresolved_errors": []}
        item = planning_chat.merge_or_save_proposal(project_id, proposal, "updates", actor=actor or "updates")
    msg = planning_chat._put_message(project_id, "assistant", text, proposalId=item["proposalId"] if item else None,
                                     kind="updates_digest")
    for s in sess:
        planning_chat._set(project_id, s["itemId"], consolidatedAt=now.isoformat(),
                           proposalId=item["proposalId"] if item else "")
    planning_chat.table().put_item(Item={**_state(project_id), "projectId": project_id, "itemId": "updstate",
                                         "lastDigestAt": now.isoformat()})
    if notify and proj.get("userId"):
        title = f"Resumen de updates: {proj.get('name', '')}" + (" (hay urgentes)" if b["urgent"] else "")
        _notify(proj["userId"], proj, title, text[:1500], "updates_digest")
        if send_external:
            checkin._notify_owner_outside(proj, title, text.split("\n")[1:40])
    return {"status": "ok", "sessions": len(sess), "proposalId": item["proposalId"] if item else None,
            "messageId": msg["itemId"], "digest": text,
            "build": {k: b[k] for k in ("counts", "contradictions", "not_reported", "reported")}
            | {"urgent": [i.get("text") for i in b["urgent"]], "decisions": [i.get("text") for i in b["decisions"]]}}


def consolidate_due(now: Optional[datetime] = None, send_external: bool = True, force: bool = False,
                    only: str = "") -> dict:
    """Hourly cron: once a day, from DIGEST_HOUR_UTC on (or 36 h after the
    last digest, so a missed hour never skips a day), each project with closed, unconsolidated updates gets its digest."""
    from agent.tools import projects_table
    from api.deps import scan_all_pages
    now = now or datetime.utcnow()
    done = []
    for proj in scan_all_pages(projects_table):
        pid = proj.get("projectId")
        if not pid or proj.get("status") in ("archived", "completed", "closed"):
            continue
        sess = member_updates.sessions(pid)
        if not sess or (only and not any((x.get("email") or "").lower() == only for x in sess)):
            continue
        last = (_state(pid).get("lastDigestAt") or "")
        last_dt = datetime.fromisoformat(last) if last else None
        if not force:
            if last_dt and last_dt.date() == now.date():
                continue
            if now.hour < DIGEST_HOUR_UTC and not (last_dt and now - last_dt >= timedelta(hours=36)):
                continue
        try:
            r = consolidate(pid, actor="cron", send_external=send_external)
            done.append({"projectId": pid, "status": r.get("status"), "proposalId": r.get("proposalId")})
        except Exception as e:
            done.append({"projectId": pid, "status": "error", "error": str(e)[:200]})
    return {"projects": done}
