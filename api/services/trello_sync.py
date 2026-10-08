"""The ONE path from a change in OneBox to the project's Trello board.

RULE: a board belongs to the project and is reached with the OWNER's Trello
connection, whoever made the change. Members never connect Trello: they edit
tasks or report their status in OneBox, and the board follows.

    task created / edited / deleted (UI or agent)  ─┐
    proposal approved (planning_chat.decide)       ─┼─> trello_reflect.reflect
    export on demand (push_tasks_to_trello)        ─┘     (owner's token)

The other direction is a READ, never an order: what people did on the board
is pulled by consolidation and by the planning chat (also with the owner's
token) and becomes a proposal the owner approves.

Nothing here raises. OneBox is the source of truth and Trello is a mirror: a
board that cannot be written must never fail the change that was just saved.
"""
import threading
from typing import List, Optional

from agent import obs


def sync_now(project_id: str, task_ids: List[str], before: Optional[List[dict]] = None,
             created: bool = False, comment: str = "") -> dict:
    """Bring the cards of these tasks in line with OneBox. `before` is each
    task as it was BEFORE the change (needed to tell an edit made in Trello
    from the one made here, and to find the card of a deleted task)."""
    try:
        from agent.tools import projects_table, tasks_table
        from api.services import trello_reflect

        proj = projects_table.get_item(Key={"projectId": project_id}).get("Item") or {}
        if not proj.get("trelloBoardId") or not proj.get("trelloListId"):
            return {"status": "not_linked"}
        owner = proj.get("userId", "")
        if not owner:
            return {"status": "error", "errors": ["project has no owner"]}

        ids = [t for t in task_ids if t]
        old = {t["taskId"]: t for t in (before or []) if t and t.get("taskId")}
        after = {}
        for tid in ids:
            item = tasks_table.get_item(Key={"projectId": project_id, "taskId": tid}).get("Item")
            if item:
                after[tid] = item
        res = trello_reflect.reflect(owner, proj, old, after, ids, ids if created else [])
        if comment:
            # The reason travels with the change: on the card, as a comment.
            from api.services import trello as trello_service
            for tid in ids:
                card_id = (after.get(tid) or {}).get("trelloCardId")
                if card_id:
                    trello_service.add_comment(owner, card_id, comment)
        if res.get("status") not in ("ok", "not_linked"):
            obs.warn("trello_sync_incomplete", project_id=project_id, status=res.get("status"),
                     errors=res.get("errors"), skipped=res.get("skipped"))
        return res
    except Exception as e:
        obs.warn("trello_sync_failed", project_id=project_id, error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"status": "error", "errors": [str(e)[:200]]}


def sync_async(project_id: str, task_ids: List[str], before: Optional[List[dict]] = None,
               created: bool = False, comment: str = "") -> None:
    """Same, without making the person who edited the task wait for Trello."""
    threading.Thread(target=sync_now, args=(project_id, list(task_ids), before, created, comment),
                     daemon=True).start()


# ── Board invitations: a log on the project, and a retry that is safe ───────
# `trelloInvites` on the project row is the record of who has been invited to
# its board: one entry per participant, {name, email, status, at, error} with
# status invited | failed | no_email. It is rebuilt from the CURRENT
# participants on every run, so it is safe to call whenever they change:
# someone already invited is not invited again, someone who failed is retried,
# and someone who had no email is invited the moment they get one.

def invite_missing(project_id: str) -> dict:
    """Invite to the project's board everyone who has not been invited yet.
    Returns {status, invited (new), already, failed, without_email}."""
    invited: List[str] = []
    failed: List[dict] = []
    without_email: List[str] = []
    out: dict = {"status": "ok", "invited": invited, "already": 0, "failed": failed,
                 "without_email": without_email}
    try:
        import time
        from datetime import datetime
        from agent.tools import projects_table
        from api.services import trello as trello_service
        from api.services.projects import normalize_participants

        proj = projects_table.get_item(Key={"projectId": project_id}).get("Item") or {}
        board, owner = proj.get("trelloBoardId"), proj.get("userId", "")
        if not board or not owner:
            return {**out, "status": "not_linked"}
        owner_email = ""
        try:
            from api.services.checkin import _owner_email
            owner_email = (_owner_email(owner) or "").lower()
        except Exception:
            pass          # worst case the owner is invited to their own board

        prev = {e.get("email"): e for e in (proj.get("trelloInvites") or []) if e.get("email")}
        now, log, seen = datetime.utcnow().isoformat(), [], set()
        for p in normalize_participants(proj.get("participants")):
            email, name = (p.get("email") or "").strip().lower(), p.get("name") or ""
            if not email:
                if name:
                    log.append({"name": name, "email": "", "status": "no_email", "at": now})
                    without_email.append(name)
                continue
            if email == owner_email or email in seen:
                continue
            seen.add(email)
            if (prev.get(email) or {}).get("status") == "invited":
                log.append(prev[email])
                out["already"] += 1
                continue
            r = trello_service.invite_member(owner, board, email, name)
            if r.get("ok"):
                log.append({"name": name, "email": email, "status": "invited", "at": now})
                invited.append(email)
            else:
                err = (r.get("error") or "")[:200]
                log.append({"name": name, "email": email, "status": "failed", "at": now, "error": err})
                failed.append({"email": email, "error": err})
            time.sleep(0.12)

        projects_table.update_item(
            Key={"projectId": project_id},
            UpdateExpression="SET trelloInvites = :l, trelloInvitesAt = :t",
            ExpressionAttributeValues={":l": log, ":t": now})
        if out["failed"]:
            obs.warn("trello_invites_failed", project_id=project_id, failed=out["failed"])
        return out
    except Exception as e:
        obs.warn("trello_invites_error", project_id=project_id, error=f"{type(e).__name__}: {str(e)[:200]}")
        return {**out, "status": "error", "failed": out["failed"] + [{"email": "", "error": str(e)[:200]}]}


def invite_missing_async(project_id: str) -> None:
    """Called whenever a project's participants change."""
    threading.Thread(target=invite_missing, args=(project_id,), daemon=True).start()
