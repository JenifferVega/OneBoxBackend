"""After the owner approves a proposal: bring the linked Trello board in line.

Only what the approved operations touched is written, and only on a project
whose board is linked (trelloBoardId + trelloListId, saved by the first export):

    created task                 -> new card in the project's list
    updated task with a card     -> name / description / due rewritten
    status done | blocked        -> card moved to the 'Hecho' / 'Bloqueado'
                                    column when the board has one
    reopened / unblocked         -> card back to the project's list
    deleted task with a card     -> card ARCHIVED (restorable in Trello)

Same safety rule as the export: a card someone edited in Trello since the
last sync (three-way hash) is NOT overwritten; it is reported instead. A
card linked by hand and never synced keeps its title and description; only
the approved date and column are written.
Trello failures never undo the approval: OneBox is the source of truth and
the result says what could not be written.
"""
from typing import Dict, List, Optional

from agent.planning.sources import norm

DONE_WORDS = ("hecho", "done", "terminad", "completad", "finaliz", "cerrad", "listo")
BLOCKED_WORDS = ("bloque", "block", "imped", "esperando", "waiting")


def _find_list(lists: List[dict], words) -> Optional[dict]:
    return next((x for x in lists if any(w in norm(x["name"]) for w in words)), None)


def reflect(uid: str, proj: dict, before: Dict[str, dict], after: Dict[str, dict], touched: List[str],
            created: List[str]) -> dict:
    """before/after: taskId -> task (after = as written in the app).
    Returns {status, created, updated, moved, archived, skipped, errors}."""
    from agent.tools.trello import _card_desc, _card_fingerprint, _clear_task_card, _diff_task_and_card, _save_task_card
    from api.services import trello as trello_service
    res: dict = {"status": "ok", "created": [], "updated": [], "moved": [], "archived": [], "skipped": [], "errors": []}
    board, home = proj.get("trelloBoardId"), proj.get("trelloListId")
    if not board or not home:
        return {**res, "status": "not_linked"}
    pid = proj["projectId"]
    try:
        lists = trello_service.list_board_lists(uid, board)
        cards = {}
        for lst in lists:
            for c in trello_service.list_cards(uid, lst["listId"]):
                cards[c["cardId"]] = c
    except Exception as e:
        return {**res, "status": "error", "errors": [f"No pude leer el tablero: {str(getattr(e, 'detail', e))[:160]}"]}
    done_l, blocked_l = _find_list(lists, DONE_WORDS), _find_list(lists, BLOCKED_WORDS)
    status_lists = {x["listId"] for x in (done_l, blocked_l) if x}

    def target_list(t: dict, current: str) -> str:
        st = t.get("status")
        if st == "done" and done_l:
            return done_l["listId"]
        if st == "blocked" and blocked_l:
            return blocked_l["listId"]
        if current in status_lists:          # reopened / unblocked: back home
            return home
        return ""

    for tid in sorted(set(touched) | set(created)):
        t, old = after.get(tid), before.get(tid)
        label = ((t or old or {}).get("text") or "")[:60]
        try:
            if t is None:                                  # deleted in the app
                card_id = (old or {}).get("trelloCardId")
                if card_id and card_id in cards:
                    trello_service.update_card(uid, card_id, closed=True)
                    res["archived"].append(label)
                continue
            name, desc, due = (t.get("text") or "").strip(), _card_desc(t), t.get("dueDate") or ""
            fp = _card_fingerprint(name, desc, due)
            card_id = t.get("trelloCardId")
            if not card_id:
                if tid in created:
                    lst = target_list(t, "") or home
                    c = trello_service.create_card(uid, lst, name, desc, due)
                    _save_task_card(pid, tid, c["cardId"], c.get("url", ""), fp, lst)
                    res["created"].append(label)
                continue
            card = cards.get(card_id)
            if card is None:
                _clear_task_card(pid, tid)
                res["skipped"].append(f"{label}: la tarjeta ya no existe en Trello")
                continue
            # compare against the task as it was BEFORE the approval: a card
            # edited in Trello since the last sync is somebody else's work
            # (a card only dragged to another column is not an edit: the
            # column is decided below from the approved status)
            d = _diff_task_and_card(old or t, card)
            base = (old or t).get("trelloSyncHash", "")
            if base and d["remote_hash"] != base:
                res["skipped"].append(f"{label}: alguien la cambió en Trello ({', '.join(d['fields'])}); no la toqué")
                continue
            move = target_list(t, card.get("listId", ""))
            if move == card.get("listId"):
                move = ""
            if not base:
                # Linked but never synced from here (a board the team wrote by
                # hand): their titles and descriptions are kept; only what the
                # approval decided -- the date and the column -- is written.
                new_due = due if due and due != (card.get("due") or "") else ""
                if new_due or move:
                    trello_service.update_card(uid, card_id, due=new_due, list_id=move)
                    (res["moved"] if move else res["updated"]).append(label)
                continue
            if fp != base or move:
                trello_service.update_card(uid, card_id, name=name, desc=desc, due=due, list_id=move)
                _save_task_card(pid, tid, card_id, t.get("trelloCardUrl", ""), fp, move or card.get("listId", ""))
                (res["moved"] if move else res["updated"]).append(label)
        except Exception as e:
            res["errors"].append(f"{label}: {str(getattr(e, 'detail', e))[:120]}")
    if res["errors"]:
        res["status"] = "partial"
    return res


def summary(res: dict) -> str:
    if not res or res.get("status") == "not_linked":
        return ""
    if res.get("status") == "error":
        return "Trello: " + "; ".join(res.get("errors") or [])
    parts = [f"{len(res[k])} {lab}" for k, lab in (("created", "tarjetas nuevas"), ("updated", "actualizadas"),
                                                    ("moved", "movidas de columna"), ("archived", "archivadas"))
             if res.get(k)]
    txt = "Trello: " + (", ".join(parts) or "sin cambios")
    if res.get("skipped"):
        txt += ". Sin tocar: " + "; ".join(res["skipped"][:5])
    if res.get("errors"):
        txt += ". Errores: " + "; ".join(res["errors"][:5])
    return txt + "."
