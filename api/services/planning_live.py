"""The planning graph on REAL projects: every path that analyses new material
(upload an attachment, paste text, create a project from a document / text /
WhatsApp / chat) ends here, through generate_insights_for_project.

    run_document_planning(project_id, owner_uid, source_name, text)
        snapshot from DynamoDB  ->  run_planning(trigger='document')
        ->  first plan of the project (no tasks yet): apply_proposal + write
            the diff to DynamoDB;  existing plan: PROPOSAL left pending in
            the planning chat (api/services/planning_chat.py) for the leader
        ->  log (proposal + events + trace) to S3  ->  notification

It runs in a background thread: the graph takes minutes and the HTTP request
that uploaded the file must not wait. One run per project at a time.

PLANNING_ENGINE=off switches every path back to the old task writer.
"""
import json
import os
import threading
import uuid
from datetime import datetime
from typing import Dict, Optional

from boto3.dynamodb.conditions import Attr, Key

TASK_FIELDS = ("text", "status", "assignedTo", "startDate", "dueDate", "dateStatus",
               "dependsOn", "blockedReason", "updatedAt")
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def enabled() -> bool:
    return os.environ.get("PLANNING_ENGINE", "on").lower() not in ("off", "0", "false")


def chat_enabled() -> bool:
    """Uploads to a project that already has a plan leave a PROPOSAL in the
    planning chat (table onebox-planning). PLANNING_UPLOADS=apply applies
    them directly, as before the chat existed."""
    return os.environ.get("PLANNING_UPLOADS", "proposal").lower() != "apply"


def _lock(project_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(project_id, threading.Lock())


# ── snapshot ────────────────────────────────────────────────────────────────

_TEXT_CACHE: dict = {}       # s3Key -> extracted text: a stored file never changes
_TEXT_CACHE_MAX = 300


def _cached_text(a: dict, read) -> dict:
    key = a.get("s3Key") or ""
    if key and key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    out = read(a)
    if key and out.get("fullText"):
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
            _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
        _TEXT_CACHE[key] = out
    return out


def snapshot(project_id: str, new_sources=(), read_all: bool = False) -> Optional[dict]:
    """Export-shaped state of the project for the graph: project, tasks,
    planning notes and attachments. Only chat exports (.txt) are read from S3
    (the WhatsApp delta needs the earlier exports) unless read_all; the new
    material [(name, text)] is passed in memory, so it does not matter whether
    its attachment row exists yet."""
    from agent.tools import insights_table, projects_table, tasks_table
    from api.deps import attachments_table
    from api.services.project_export import _attachment_text, _query_all

    proj = projects_table.get_item(Key={"projectId": project_id}).get("Item")
    if not proj:
        return None
    owner = proj.get("userId", "")
    tasks = _query_all(tasks_table, KeyConditionExpression=Key("projectId").eq(project_id))
    insights = _query_all(insights_table, KeyConditionExpression=Key("userId").eq(owner),
                          FilterExpression=Attr("projectId").eq(project_id) & Attr("type").eq("note")) if owner else []
    atts = _query_all(attachments_table, KeyConditionExpression=Key("projectId").eq(project_id))
    names = {n for n, _t in new_sources}
    atts = [a for a in atts if (a.get("fileName") or "") not in names]
    for a in atts:
        if read_all or (a.get("extension") or "").lower() == "txt":
            a.update(_cached_text(a, _attachment_text))
    atts.sort(key=lambda a: a.get("createdAt", ""))
    now = datetime.utcnow().isoformat()
    atts += [{"fileName": n, "fullText": t, "createdAt": now} for n, t in new_sources]
    return {"project": proj, "tasks": tasks, "insights": insights, "attachments": atts}


def build_snapshot(project_id: str, source_name: str, text: str) -> Optional[dict]:
    return snapshot(project_id, [(source_name, text)])


# ── persist ─────────────────────────────────────────────────────────────────

def _clean(v):
    """DynamoDB rejects floats; numbers become strings, lists stay lists."""
    if isinstance(v, float):
        return str(v)
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def persist(before: dict, after: dict, proposal: dict, owner_uid: str, now: str,
            tasks_table, insights_table, projects_table) -> dict:
    """Write the difference between two exports. New ids ('new-1'...) become
    real uuids everywhere they are referenced. Returns counts + the id map."""
    project_id = (before.get("project") or {}).get("projectId", "")
    old = {t["taskId"]: t for t in before.get("tasks") or []}
    new_tasks = after.get("tasks") or []
    idmap = {t["taskId"]: uuid.uuid4().hex for t in new_tasks if t["taskId"] not in old}
    remap = lambda x: idmap.get(x, x)  # noqa: E731
    counts = {"created": 0, "updated": 0, "deleted": 0, "notes": 0}

    keep = set()
    for t in new_tasks:
        tid = remap(t["taskId"])
        keep.add(tid)
        deps = [remap(d) for d in t.get("dependsOn") or []]
        if t["taskId"] in idmap:
            item = {"projectId": project_id, "taskId": tid, "userId": owner_uid,
                    "text": (t.get("text") or "")[:500], "status": t.get("status") or "pending",
                    "createdBy": "IA", "assignedTo": t.get("assignedTo") or "",
                    "startDate": t.get("startDate") or "", "dueDate": t.get("dueDate") or "",
                    "dateStatus": t.get("dateStatus") or "", "dependsOn": deps,
                    "blockedReason": t.get("blockedReason") or "", "sourceLabel": "planning",
                    "proposalId": proposal.get("proposalId", ""), "createdAt": now, "updatedAt": now}
            tasks_table.put_item(Item={k: _clean(v) for k, v in item.items()})
            counts["created"] += 1
            continue
        prev = old[t["taskId"]]
        cur = dict(t, dependsOn=deps)
        changed = {f: cur.get(f) for f in TASK_FIELDS
                   if f in cur and cur.get(f) != prev.get(f) and not (cur.get(f) in ("", None, []) and f not in prev)}
        if not changed:
            continue
        names = {f"#f{i}": f for i, f in enumerate(changed)}
        values = {f":v{i}": _clean(v if v is not None else "") for i, v in enumerate(changed.values())}
        tasks_table.update_item(Key={"projectId": project_id, "taskId": tid},
                                UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(changed))),
                                ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        counts["updated"] += 1
    for tid in old:
        if tid not in keep:
            tasks_table.delete_item(Key={"projectId": project_id, "taskId": tid})
            counts["deleted"] += 1

    # Planning notes + one feed entry per created / closed task.
    seen = {i.get("insightId") for i in before.get("insights") or []}
    pname = (before.get("project") or {}).get("name", "")
    for ins in after.get("insights") or []:
        if ins.get("insightId") in seen:
            continue
        item = dict(ins, userId=owner_uid, taskId=remap(ins.get("taskId", "")))
        insights_table.put_item(Item={k: _clean(v) for k, v in item.items()})
        counts["notes"] += 1
    by_id = {t["taskId"]: t for t in new_tasks}
    for i, o in enumerate(proposal.get("operations") or [], 1):
        if o.get("op") not in ("create", "close"):
            continue
        t = by_id.get(o.get("task_id")) if o.get("op") == "close" else None
        title = o.get("text") or (t or old.get(o.get("task_id"), {})).get("text", "")
        insights_table.put_item(Item={
            "userId": owner_uid, "insightId": f"{now}#plan{i:03d}{uuid.uuid4().hex[:4]}",
            "projectId": project_id, "projectName": pname,
            "type": "task_created" if o["op"] == "create" else "work_done",
            "title": title[:300], "description": (o.get("reason") or "")[:500],
            "actionsTaken": ["task_created" if o["op"] == "create" else "task_closed"],
            "status": "created", "source": "planning", "createdAt": now})

    proj_after = after.get("project") or {}
    projects_table.update_item(Key={"projectId": project_id},
                               UpdateExpression="SET planVersion = :v, lastActivity = :t",
                               ExpressionAttributeValues={":v": int(proj_after.get("planVersion") or 1), ":t": now})
    return {**counts, "idmap": idmap}


def _log_to_s3(project_id: str, stamp: str, payload: dict) -> str:
    from agent.document_parser import S3_ATTACHMENTS_BUCKET, get_s3_client
    key = f"planning/{project_id}/{stamp.replace(':', '-')}.json"
    get_s3_client().put_object(Bucket=S3_ATTACHMENTS_BUCKET, Key=key, ContentType="application/json",
                               Body=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    return key


def _notify(owner_uid: str, project: dict, title: str, message: str, ntype: str) -> None:
    from agent.tools import notifications_table
    try:
        notifications_table.put_item(Item={
            "userId": owner_uid, "notificationId": f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
            "projectId": project.get("projectId", ""), "projectName": project.get("name", ""),
            "type": ntype, "title": title, "message": message, "channel": "system",
            "status": "unread", "createdAt": datetime.utcnow().isoformat()})
    except Exception as e:
        print(f"[planning_live] notification error: {e}")


# ── entry points ────────────────────────────────────────────────────────────

def run_document_planning(project_id: str, owner_uid: str, source_name: str, text: str,
                          llm=None) -> dict:
    """Synchronous: snapshot -> graph -> apply (first plan) or propose. Returns a summary."""
    from agent.planning.runner import run_planning
    from agent.planning.schedule import apply_proposal
    from agent.tools import insights_table, projects_table, tasks_table

    with _lock(project_id):
        t0 = datetime.utcnow()
        before = build_snapshot(project_id, source_name, text)
        if not before:
            return {"ok": False, "reason": "project not found"}
        owner_uid = owner_uid or before["project"].get("userId", "")
        res = run_planning(before, "document", document=source_name, mode="delta", llm=llm)
        proposal = res.get("proposal")
        stamp = datetime.utcnow().isoformat()
        summary: dict = {"ok": True, "operations": 0}
        first_plan = not before.get("tasks")
        events: list = []
        has_content = bool(proposal and (proposal.get("operations") or proposal.get("notes")))
        if has_content and (first_plan or not chat_enabled()):
            # A new project has nothing to break: its first plan is applied.
            after, events = apply_proposal(before, proposal, approved=None, actor="auto", now=stamp)
            summary = {"ok": True, "operations": len(proposal.get("operations") or []), "applied": True,
                       **persist(before, after, proposal, owner_uid, stamp,
                                 tasks_table, insights_table, projects_table)}
        elif has_content or (proposal and proposal.get("questions")):
            summary = {"ok": True, "operations": len(proposal.get("operations") or []), "applied": False}
        summary["questions"] = [q.get("text", "") for q in (proposal or {}).get("questions") or []]
        summary["seconds"] = round((datetime.utcnow() - t0).total_seconds(), 1)
        log_key = ""
        try:
            log_key = summary["log"] = _log_to_s3(project_id, stamp, {
                "source": source_name, "proposal": proposal, "events": events, "facts": res.get("facts"),
                "topics": res.get("topics"), "trace": res.get("trace"),
                "summary": {k: v for k, v in summary.items() if k != "idmap"}})
        except Exception as e:
            print(f"[planning_live] S3 log error: {e}")
        c = summary
        if c.get("applied"):
            msg = (f"{c.get('created', 0)} nuevas, {c.get('updated', 0)} actualizadas, "
                   f"{c.get('deleted', 0)} eliminadas, {c.get('notes', 0)} notas")
            if c["questions"]:
                msg += ". Preguntas: " + " | ".join(c["questions"][:3])
            _notify(owner_uid, before["project"], f"Plan actualizado con {source_name}", msg, "plan_updated")
        elif c.get("applied") is False:
            # Existing plan: the change waits for the leader in the planning chat.
            from api.services import planning_chat
            item = planning_chat.save_proposal(project_id, proposal, f"upload:{source_name}", actor="auto",
                                               log_key=log_key)
            planning_chat._put_message(project_id, "assistant",
                                       f"Analicé «{source_name}». " + planning_chat._describe(proposal),
                                       proposalId=item["proposalId"], sources=[source_name])
            summary["proposalId"] = item["proposalId"]
            _notify(owner_uid, before["project"], f"Propuesta de plan lista: {source_name}",
                    planning_chat._describe(proposal) + " Revísala en Intelligence > Replanificación.",
                    "plan_proposal")
        print(f"[planning_live] {project_id} {source_name}: {json.dumps({k: v for k, v in summary.items() if k != 'idmap'}, default=str)}")
        return summary


def start_document_planning(project_id: str, owner_uid: str, source_name: str, text: str) -> bool:
    """Fire-and-forget. Errors are logged and notified, never raised to the caller."""
    if not enabled() or not project_id or not (text or "").strip():
        return False

    def _job():
        try:
            run_document_planning(project_id, owner_uid, source_name, text)
        except Exception as e:
            import traceback
            traceback.print_exc()
            _notify(owner_uid, {"projectId": project_id}, f"No se pudo actualizar el plan con {source_name}",
                    str(e)[:300], "plan_error")

    threading.Thread(target=_job, name=f"planning-{project_id}", daemon=True).start()
    return True
