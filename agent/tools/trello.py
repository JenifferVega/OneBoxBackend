"""Trello agent tools: list boards and push a project's tasks as cards.

SCOPE: one direction only, OneBox -> Trello, on demand.

Re-running a push does NOT duplicate cards: the Trello card id is stored on
the task itself (`trelloCardId`), so a task that already has one is skipped.
That is the whole mapping this one-way sync needs -- no link table, no echo
suppression, no conflict policy. Those only become necessary the day changes
have to travel back from Trello.
"""
import time

from agent.tools.access import _has_project_access
from agent.tools.context import _current_email, _current_uid
from agent.tools.db import projects_table, tasks_table
from agent.tools.errors import _svc_error
from agent.tools.registry import register_tool

# Trello allows 100 requests per 10s per token. One card is one request, so a
# small pause keeps a large project from tripping the limit mid-push and
# leaving half the tasks pushed.
_PUSH_PAUSE = 0.12


@register_tool("list_trello_boards")
def list_trello_boards() -> dict:
    """Lists the user's Trello boards WITH their lists, so the user can say
    where the tasks should go.

    In Trello the list (column) IS the status, so a card always needs a
    target list -- there is no "default" place for it.
    """
    try:
        from api.services import trello as trello_service
        uid = _current_uid()
        boards = trello_service.list_boards(uid)
        for b in boards:
            try:
                b["lists"] = trello_service.list_board_lists(uid, b["boardId"])
            except Exception as e:
                b["lists"] = []
                b["listsError"] = str(e)[:120]
        return {
            "count": len(boards), "boards": boards,
            # Whoever asks "where do these cards go?" must see BOTH answers.
            # This used to live only in the error push_tasks_to_trello returns
            # when a project is not linked -- so on any route that listed the
            # boards WITHOUT calling the push first, creating a board was never
            # offered and the user was shown existing boards as if they were
            # the only choice. Same fact, every route, because it is data.
            "also_available": {
                "option": "create_new",
                "tool": "create_trello_board",
                "how": "Create a NEW board for the project with OneBox's own "
                       "statuses as columns (Pendiente / En curso / Bloqueado / "
                       "Hecho), which keeps list and status in step. REQUIRES "
                       "the user to agree first.",
            },
            "instruction": "When the user has not said where the cards go, "
                           "offer BOTH: one of the boards listed here, or a new "
                           "board for this project. Having boards already does "
                           "not mean they want this project's cards in one.",
        }
    except Exception as e:
        return {"error": _svc_error(e)}


def _resolve_list_id(uid: str, list_id: str = "", list_name: str = "",
                     board_name: str = "") -> tuple:
    """Turn a list NAME into a list id. Returns (list_id, error_dict).

    The planner cannot do this itself: list_trello_boards returns boards that
    CONTAIN lists, and the executor's from_step extraction is flat -- it has
    no two-level match. Asked for "the MVP list of the ONEBOX board" it kept
    inventing syntax that does not exist (`then_match`, `board_name`...) and
    burned every retry on it.

    Matching is case-insensitive and by substring, because people say "MVP"
    for a list actually called "MVP - TO DO".
    """
    from api.services import trello as trello_service

    if (list_id or "").strip():
        return list_id.strip(), None
    if not (list_name or "").strip():
        # The two options live HERE, in the data, not in a prompt. Whoever
        # composes the reply -- planner on a direct_response, narrator on an
        # executed plan -- reads this. Putting it in one prompt only meant the
        # other path never offered creating a board.
        return "", {
            "error": "This project has no Trello list yet and none was given. "
                     "Ask the user which of TWO options they want.",
            "needs_user_choice": True,
            "options": [
                {"option": "use_existing",
                 "how": "They pick one of their boards and a list on it; then "
                        "call push_tasks_to_trello with list_name and board_name.",
                 "tool": "push_tasks_to_trello"},
                {"option": "create_new",
                 "how": "Create a board for this project with OneBox's own "
                        "statuses as columns (Pendiente / En curso / Bloqueado / "
                        "Hecho). REQUIRES the user to agree first.",
                 "tool": "create_trello_board"},
            ],
            "instruction": "Offer BOTH options. Having boards already does not "
                           "mean they want this project's cards in one of them.",
        }

    want_list = list_name.strip().lower()
    want_board = (board_name or "").strip().lower()

    matches, available = [], []
    for b in trello_service.list_boards(uid):
        if want_board and want_board not in b["name"].lower():
            continue
        for l in trello_service.list_board_lists(uid, b["boardId"]):
            available.append(f'{b["name"]} / {l["name"]}')
            if want_list in l["name"].lower() or l["name"].lower() in want_list:
                matches.append({"listId": l["listId"], "list": l["name"],
                                "board": b["name"]})

    if len(matches) == 1:
        return matches[0]["listId"], None
    if not matches:
        return "", {"error": f"No list matching '{list_name}'"
                             + (f" on a board matching '{board_name}'" if board_name else "")
                             + ". Available lists: " + "; ".join(available[:30]),
                    "available_lists": available}
    return "", {"error": f"'{list_name}' matches {len(matches)} lists. Ask the user "
                         f"which board, or pass board_name.",
                "matches": matches}


# ── Remembering which board a project belongs to ────────────────────────────
# Without this the agent has to ask "which list?" on every single push. Once
# is helpful; the third time it is the reason someone stops using the feature.

def _project_link(project_id: str) -> dict:
    """The Trello board/list this project is linked to, or {}."""
    item = projects_table.get_item(Key={"projectId": project_id}).get("Item") or {}
    if not item.get("trelloListId"):
        return {}
    return {
        "boardId":   item.get("trelloBoardId", ""),
        "boardName": item.get("trelloBoardName", ""),
        "listId":    item.get("trelloListId", ""),
        "listName":  item.get("trelloListName", ""),
    }


def _card_desc(task: dict) -> str:
    """The card body for a task. Built in ONE place because it is written on
    create and rewritten on every re-export; two copies would drift and every
    export would then look like a change."""
    parts = []
    if task.get("description"):
        parts.append(task["description"])
    if task.get("assignedTo"):
        # Trello needs a member id to really assign; the name alone cannot do
        # it, so it is recorded in the description instead of silently
        # dropping the information.
        parts.append(f"Owner (OneBox): {task['assignedTo']}")
    if task.get("status") == "blocked":
        parts.append("Status in OneBox: BLOCKED")
    parts.append(f"OneBox task: {task.get('taskId', '')}")
    return "\n\n".join(parts)


def _card_fingerprint(name: str, desc: str, due: str) -> str:
    """What was last written to the card. Re-exporting compares against this
    and calls Trello only for the tasks that actually changed -- otherwise
    every export would be one API request per task and hit the rate limit
    that already bit us at four tasks."""
    import hashlib
    return hashlib.sha256(
        "\x00".join((name or "", desc or "", due or "")).encode("utf-8")
    ).hexdigest()[:32]


# ── Comparing OneBox against Trello ─────────────────────────────────────────
# Two values that differ tell you nothing about WHO moved. Three do. The hash
# saved on the last sync is the common ancestor -- the same trick git uses --
# and it turns a confusing "they are different" into exactly one of four
# answers:
#
#                     | card == base        | card != base
#   task == base      | nothing to do       | changed in Trello
#   task != base      | changed in OneBox   | CONFLICT
#
# Only the last one needs a human. Without the base, every difference looks
# like that last box, and a one-way push silently resolves all of them in
# OneBox's favour -- overwriting whatever a person did in Trello.

def _diff_task_and_card(task: dict, card: dict) -> dict:
    """Which side moved, and on what. Pure function: no I/O, so it can be
    tested without Trello or DynamoDB."""
    base = task.get("trelloSyncHash", "")
    local_name = (task.get("text") or "").strip()
    local_desc = _card_desc(task)
    local_due = task.get("dueDate", "") or ""
    local = _card_fingerprint(local_name, local_desc, local_due)

    remote = _card_fingerprint(card.get("name", ""), card.get("desc", ""),
                               card.get("due", ""))

    local_changed = bool(base) and local != base
    remote_changed = bool(base) and remote != base
    # A card dragged to another column is how people change status in Trello.
    # The column cannot always be read as a status -- on a board we did not
    # create, the names are someone else's convention -- but the MOVE itself
    # is still a change the user should hear about.
    moved = bool(task.get("trelloCardListId")) and \
        card.get("listId", "") != task.get("trelloCardListId")

    if not base:
        # Exported before this field existed: adopt the current state as the
        # base instead of guessing that someone changed something.
        verdict = "adopt_base"
    elif local_changed and (remote_changed or moved):
        verdict = "conflict"
    elif local_changed:
        verdict = "local"
    elif remote_changed or moved:
        verdict = "remote"
    else:
        verdict = "unchanged"

    fields = []
    if card.get("name", "") != local_name:
        fields.append("title")
    if card.get("desc", "") != local_desc:
        fields.append("description")
    if card.get("due", "") != local_due:
        fields.append("due date")
    if moved:
        fields.append("column")

    return {"verdict": verdict, "fields": fields, "local_hash": local,
            "remote_hash": remote, "moved": moved,
            "lastActivity": card.get("lastActivity", "")}


def _save_task_card(project_id: str, task_id: str, card_id: str,
                    url: str, fingerprint: str, list_id: str = "") -> None:
    """Record the new common ancestor. Written ONLY after both sides agree,
    because a base saved while they still differ erases the evidence of who
    moved and makes the next comparison lie."""
    tasks_table.update_item(
        Key={"projectId": project_id, "taskId": task_id},
        UpdateExpression=("SET trelloCardId = :c, trelloCardUrl = :u, "
                          "trelloSyncHash = :h, trelloCardListId = :l"),
        ExpressionAttributeValues={":c": card_id, ":u": url, ":h": fingerprint,
                                   ":l": list_id},
    )


def _clear_task_card(project_id: str, task_id: str) -> None:
    """Forget a card the user deleted in Trello, so the next export recreates
    it instead of failing on it forever."""
    try:
        tasks_table.update_item(
            Key={"projectId": project_id, "taskId": task_id},
            UpdateExpression=("REMOVE trelloCardId, trelloCardUrl, "
                              "trelloSyncHash"),
        )
    except Exception as e:
        print(f"[Tool] could not clear stale Trello link on {task_id}: {e}")


def _save_project_link(project_id: str, board: dict, lst: dict) -> None:
    projects_table.update_item(
        Key={"projectId": project_id},
        UpdateExpression=("SET trelloBoardId = :b, trelloBoardName = :bn, "
                          "trelloListId = :l, trelloListName = :ln"),
        ExpressionAttributeValues={
            ":b": board.get("boardId", ""), ":bn": board.get("name", ""),
            ":l": lst.get("listId", ""),    ":ln": lst.get("name", ""),
        },
    )


@register_tool("prepare_trello_board")
def prepare_trello_board(project_query: str) -> dict:
    """Can a Trello board be created for this project? Answers in ONE call.

    Resolves the project by name AND reports whether it already has a board.
    Read-only: nothing is created and nothing is written.

    Call this BEFORE proposing to create a board. It exists because the two
    halves of that question used to be two steps -- resolve the project, then
    look at its link -- with a replan in between, and the planner does not
    carry a step's result across a replan. So it proposed creating a board for
    a project that had one, and asked the user to confirm a mistake.

    One call, one answer: `can_create` is true or false, and `already_linked`
    says which board it is if not.
    """
    q = (project_query or "").strip()
    if not q:
        return {"error": "Provide the project name"}

    from agent.tools.resolve import resolve_entity
    found = resolve_entity(q)
    if found.get("error"):
        return found

    pid = found.get("projectId")
    if not pid:
        # Not resolved to one project: pass the resolution verdict straight
        # through rather than inventing one here.
        return {
            "resolved": False,
            "can_create": False,
            "query": q,
            "confidence": found.get("confidence", "none"),
            "candidates": found.get("candidates", []),
            "instruction": found.get(
                "instruction",
                "The project could not be identified. Ask which one they mean "
                "before doing anything."),
        }

    link = _project_link(pid)
    out = {
        "resolved": True,
        "projectId": pid,
        "projectName": found.get("projectName") or found.get("name") or q,
        "confidence": found.get("confidence", "high"),
    }

    if link:
        out.update({
            "can_create": False,
            "already_linked": link,
            "instruction": (
                f"This project is ALREADY linked to the Trello board "
                f"'{link.get('boardName')}' (list '{link.get('listName')}'). "
                f"A project has ONE board. Tell the user which board it is and "
                f"offer to send the tasks there with push_tasks_to_trello "
                f"(no board or list argument). Do NOT propose creating another "
                f"board and do NOT ask them to confirm creating one."),
        })
        return out

    # Not linked: creating is legitimate. Everything needed to propose it is
    # here, so the proposal does not need another lookup.
    out.update({
        "can_create": True,
        "already_linked": None,
        "suggested_name": out["projectName"],
        "suggested_lists": ["Pendiente", "En curso", "Bloqueado", "Hecho"],
        "next_tool": "create_trello_board",
        "instruction": (
            "This project has NO Trello board. Creating one writes into the "
            "user's Trello account, so PROPOSE it and ask for confirmation "
            "first; only call create_trello_board after they agree."),
    })
    try:
        from api.services.tasks import list_tasks as _list_tasks
        open_tasks = [t for t in _list_tasks(_current_uid(), _current_email(), pid)
                      if (t.get("text") or "").strip() and t.get("status") != "done"]
        out["tasks_waiting_to_export"] = len(open_tasks)
    except Exception as e:
        print(f"[Tool] prepare_trello_board: could not count tasks: {e}")

    return out


@register_tool("create_trello_board")
def create_trello_board(name: str, project_id: str = "",
                        lists: list = None) -> dict:
    """Creates a NEW Trello board with the given lists. REQUIRES CONFIRMATION.

    Parameters:
      - name: board name (required).
      - project_id: if given, the project is linked to the new board and its
        first list, so later pushes do not have to ask where to put cards.
      - lists: column names in order. Defaults to OneBox's own statuses, which
        is the whole advantage of creating the board here: the list-to-status
        mapping is something we defined, not something we guessed.

    This writes into the user's Trello account and the board is visible to
    everyone on it. Never call it without the user explicitly asking.
    """
    if not (name or "").strip():
        return {"error": "Provide a name for the board"}

    # Already linked? Then this is almost certainly a repeat, not a request for
    # a SECOND board. A replan inside one turn is handled by the executor's
    # write ledger, but nothing protected the case ACROSS turns: the project
    # gets a board, the conversation continues, and a later plan creates
    # another one with the same name. In Trello both are real, and the user is
    # told about one. The saved link is the memory that settles it.
    if (project_id or "").strip():
        existing = _project_link(project_id)
        if existing:
            return {
                "error": f"This project is already linked to the Trello board "
                         f"'{existing.get('boardName')}' (list "
                         f"'{existing.get('listName')}'). Creating another board "
                         f"would leave two boards for one project.",
                "already_linked": existing,
                "next_step": {
                    "how": "Export to the board it is already linked to: call "
                           "push_tasks_to_trello with no list_name and no "
                           "board_name. New tasks get a card, tasks already "
                           "exported have their card brought up to date.",
                    "tool": "push_tasks_to_trello"},
                "instruction": "Do NOT create a second board. A project has ONE "
                               "board. Say which board it is linked to and "
                               "offer to export there.",
            }

    try:
        from api.services import trello as trello_service
        uid = _current_uid()
        cols = [c for c in (lists or ["Pendiente", "En curso", "Bloqueado", "Hecho"]) if c]
        board = trello_service.create_board(uid, name, cols)

        linked = None
        if (project_id or "").strip():
            if not _has_project_access(project_id):
                return {**board, "warning": "Board created, but you have no access "
                                            "to that project so it was not linked."}
            first = next((l for l in board["lists"] if l.get("listId")), None)
            if first:
                _save_project_link(project_id, board, first)
                linked = {"projectId": project_id, "listId": first["listId"],
                          "listName": first["name"]}

        out = {"success": True, **board, "linked": linked}

        # An empty board is not the finished job. Nobody creates a board for a
        # project in order to look at four empty columns -- it is a means to
        # getting the tasks onto it. Saying how many are waiting, and naming
        # the exact call that puts them there, keeps that fact in the DATA,
        # where the narrator and the planner both see it. Otherwise the turn
        # ends on "board created" and the tasks are quietly forgotten.
        if linked:
            try:
                from api.services.tasks import list_tasks as _list_tasks
                pending = [t for t in _list_tasks(uid, _current_email(), project_id)
                           if (t.get("text") or "").strip()
                           and t.get("status") != "done"]
                out["tasks_waiting_to_export"] = len(pending)
                if pending:
                    out["next_step"] = {
                        "tool": "push_tasks_to_trello",
                        "params": {"project_id": project_id},
                        "why": f"The board is EMPTY. {len(pending)} task(s) of "
                               f"this project still have no card.",
                    }
                    out["instruction"] = (
                        "Do NOT end saying only that the board was created. Say "
                        "it is empty and that N tasks are ready to export, and "
                        "export them if that is what the user was asking for.")
            except Exception as e:
                print(f"[Tool] could not count pending tasks: {e}")

        return out
    except Exception as e:
        return {"error": _svc_error(e)}


@register_tool("link_project_to_trello")
def link_project_to_trello(project_id: str, list_name: str = "",
                           board_name: str = "", list_id: str = "") -> dict:
    """Remembers which Trello list a project's cards go to.

    After this, push_tasks_to_trello needs no list: it uses the saved one.
    Use it when the user picks an EXISTING board instead of creating one.
    """
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    try:
        from api.services import trello as trello_service
        uid = _current_uid()
        resolved, err = _resolve_list_id(uid, list_id, list_name, board_name)
        if err:
            return err
        board, lst = None, None
        for b in trello_service.list_boards(uid):
            for l in trello_service.list_board_lists(uid, b["boardId"]):
                if l["listId"] == resolved:
                    board, lst = b, l
                    break
            if board:
                break
        if not board:
            return {"error": f"List {resolved} not found on any of your boards"}
        _save_project_link(project_id, board, {"listId": lst["listId"], "name": lst["name"]})
        print(f"[Tool] link_project_to_trello({project_id}) -> "
              f"{board['name']} / {lst['name']}")
        return {"success": True, "projectId": project_id,
                "boardName": board["name"], "listName": lst["name"],
                "listId": lst["listId"]}
    except Exception as e:
        return {"error": _svc_error(e)}

@register_tool("push_tasks_to_trello")
def push_tasks_to_trello(project_id: str, list_id: str = "",
                         list_name: str = "", board_name: str = "",
                         only_pending: bool = True) -> dict:
    """Exports a project's tasks to Trello, and keeps them in step afterwards.

    A task with no card gets one. A task that ALREADY has a card has that card
    updated to match the task (title, description, owner, blocked flag, due
    date). So exporting again is a sync, not a duplicate and not a no-op --
    which is the point: the board stays current as the project moves.

    Cards whose task has not changed since the last export are left alone and
    reported as unchanged, so a re-export costs one Trello request per task
    that actually moved rather than one per task.

    Parameters:
      - project_id: OneBox project (required).
      - list_id: target Trello list id, when you already have it.
      - list_name: target list BY NAME (e.g. "MVP"), matched case-insensitively
        and by substring, so "MVP" finds a list called "MVP - TO DO".
      - board_name: narrows list_name to one board when the name is ambiguous.
      - only_pending: if True (default), skips tasks already done.

    Give EITHER list_id OR list_name. Passing names is preferred: the tool
    resolves them against Trello itself.

    If the project has no link yet, the first successful push SAVES the list
    as that project's destination, so later pushes need no list at all.

    Returns `outcome` (complete | partial | failed | noop), a `summary` line,
    and created / updated / skipped / failed naming every task involved -- so
    the caller reports exactly what happened instead of collapsing a partial
    result into a single success flag.
    """
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    try:
        from api.services import trello as trello_service
        from api.services.tasks import list_tasks as _list_tasks

        uid = _current_uid()
        # Fall back to the list this project was linked to, so the user is
        # asked where the cards go ONCE, not on every push.
        if not any((list_id, list_name)):
            link = _project_link(project_id)
            if link:
                list_id = link["listId"]
                print(f"[Tool] using saved link -> {link['boardName']} / {link['listName']}")
        list_id, err = _resolve_list_id(uid, list_id, list_name, board_name)
        if err:
            return err
        items = _list_tasks(uid, _current_email(), project_id)

        # ONE read of the whole list, not one per task: with a card-by-card
        # read the rate limit hits at a handful of tasks.
        already_exported = [t for t in items if t.get("trelloCardId")]
        cards_by_id = {}
        try:
            cards_by_id = {c["cardId"]: c
                           for c in trello_service.list_cards(uid, list_id)}
        except Exception as e:
            # Without Trello's side there is no common ancestor, and writing
            # anyway is exactly how a person's edits get overwritten -- so a
            # RE-export refuses rather than pushing blind.
            #
            # A FIRST export is different: no task has a card yet, so there is
            # nothing on the board that this could overwrite. Blocking it would
            # only stop new cards from being created for no safety gain.
            if already_exported:
                return {
                    "error": f"Could not read the Trello list before writing to "
                             f"it: {str(e)[:160]}. Some of these tasks already "
                             f"have cards, and without reading the board first "
                             f"an update could overwrite changes made in Trello.",
                    "instruction": "Do not retry automatically. Tell the user "
                                   "the export was NOT performed."}
            print(f"[Tool] could not read the list ({str(e)[:100]}); no task has "
                  f"a card yet, so this is a first export and proceeds.")

        created, updated, skipped, failed = [], [], [], []
        remote_changes, conflicts = [], []
        for t in items:
            text = (t.get("text") or "").strip()
            if not text:
                continue

            desc = _card_desc(t)
            due = t.get("dueDate", "") or ""
            fingerprint = _card_fingerprint(text, desc, due)

            # ── Already exported: compare all three sides ──────────────
            card_id = t.get("trelloCardId")
            if card_id:
                card = cards_by_id.get(card_id)
                if card is None or card.get("closed"):
                    # Deleted or archived in Trello. Drop the stale link so the
                    # NEXT export recreates it, instead of failing forever.
                    _clear_task_card(project_id, t.get("taskId", ""))
                    failed.append({"text": text, "error":
                                   "its Trello card was deleted or archived; "
                                   "the link was cleared, export again to "
                                   "recreate it"})
                    continue

                d = _diff_task_and_card(t, card)
                v = d["verdict"]

                if v == "unchanged":
                    skipped.append({"text": text, "reason": "already up to date",
                                    "cardId": card_id,
                                    "url": t.get("trelloCardUrl", "")})
                    continue

                if v == "remote":
                    # Somebody edited the card. Leave it alone and say so.
                    # Bringing it back into OneBox is the other direction and
                    # belongs in its own module -- but silently overwriting it
                    # is the one thing that must not happen here.
                    remote_changes.append({
                        "text": text, "cardId": card_id,
                        "url": t.get("trelloCardUrl", ""),
                        "changed_in_trello": d["fields"],
                        "when": d["lastActivity"],
                        "action_taken": "none -- the card was left as it is"})
                    continue

                if v == "conflict":
                    conflicts.append({
                        "text": text, "cardId": card_id,
                        "url": t.get("trelloCardUrl", ""),
                        "changed_on_both_sides": d["fields"],
                        "onebox": {"title": text, "due": due,
                                   "status": t.get("status", "")},
                        "trello": {"title": card.get("name", ""),
                                   "due": card.get("due", ""),
                                   "moved_column": d["moved"]},
                        "when": d["lastActivity"],
                        "action_taken": "none -- nothing was written"})
                    continue

                # "local" (only OneBox moved) or "adopt_base" (exported before
                # there was a base; take the current state as the ancestor).
                if v == "adopt_base" and d["local_hash"] == d["remote_hash"]:
                    _save_task_card(project_id, t.get("taskId", ""), card_id,
                                    t.get("trelloCardUrl", ""), fingerprint,
                                    card.get("listId", ""))
                    skipped.append({"text": text, "reason": "already up to date",
                                    "cardId": card_id,
                                    "url": t.get("trelloCardUrl", "")})
                    continue

                try:
                    card_out = trello_service.update_card(
                        uid, card_id, name=text, desc=desc, due=due)
                except Exception as e:
                    failed.append({"text": text, "error": str(e)[:160]})
                    continue
                _save_task_card(project_id, t.get("taskId", ""), card_id,
                                t.get("trelloCardUrl", "") or card_out.get("url", ""),
                                fingerprint, card.get("listId", ""))
                updated.append({"text": text, "cardId": card_id,
                                "url": card_out.get("url", "") or t.get("trelloCardUrl", ""),
                                "updated_fields": d["fields"]})
                time.sleep(_PUSH_PAUSE)
                continue

            if only_pending and t.get("status") == "done":
                skipped.append({"text": text, "reason": "already done"})
                continue

            try:
                card = trello_service.create_card(
                    uid, list_id, name=text, desc=desc, due=due)
            except Exception as e:
                failed.append({"text": text, "error": str(e)[:160]})
                continue

            # Record the link so a second run does not duplicate the card.
            # If this write fails the card already exists, so we report the
            # task as failed rather than pretending it synced cleanly.
            try:
                _save_task_card(project_id, t["taskId"], card["cardId"],
                                card.get("url", ""), fingerprint, list_id)
            except Exception as e:
                failed.append({
                    "text": text,
                    "error": f"card created ({card.get('url', '')}) but the link "
                             f"could not be saved: {str(e)[:100]}. Re-running "
                             f"WILL duplicate this card.",
                })
                continue

            created.append({"text": text, "cardId": card["cardId"],
                            "url": card.get("url", "")})
            time.sleep(_PUSH_PAUSE)

        # A partial success is NOT a failure, and reporting it as one is how a
        # user ends up believing nothing happened while cards sit in Trello.
        # `success: not failed` did exactly that: two cards created and one
        # rate-limited came back as success=False, and the narrator -- rightly
        # applying "if there is an error, the action did not complete" --
        # reported only the failure and dropped the two that worked.
        #
        # `outcome` carries the nuance a boolean cannot.
        touched = created + updated
        # A conflict is not a failure and not a success: nothing broke, and
        # nothing was written either. Folding it into "complete" would hide
        # the one case that actually needs the user.
        if conflicts or remote_changes:
            outcome = "needs_review" if not failed else "partial"
        elif failed and not touched:
            outcome = "failed"
        elif failed:
            outcome = "partial"
        elif not touched and not skipped:
            outcome = "noop"
        else:
            outcome = "complete"

        summary = (f"{len(created)} created, {len(updated)} updated, "
                   f"{len(skipped)} unchanged, {len(remote_changes)} changed in "
                   f"Trello, {len(conflicts)} in conflict, {len(failed)} failed")
        # Remember where this project's cards go, WITHOUT needing the planner
        # to call link_project_to_trello afterwards. A successful push to a
        # list already answers "where do this project's cards belong", and
        # relying on the model to make a second call meant it happened some
        # times and not others -- so the next message had to spell out the
        # board and list all over again.
        #
        # Only on success, and only if the project was not linked already: a
        # one-off push to a different list must not silently retarget it.
        if created and not _project_link(project_id):
            try:
                board = next((b for b in trello_service.list_boards(uid)
                              if any(l["listId"] == list_id
                                     for l in trello_service.list_board_lists(uid, b["boardId"]))), None)
                if board:
                    lst = next(l for l in trello_service.list_board_lists(uid, board["boardId"])
                               if l["listId"] == list_id)
                    _save_project_link(project_id, board, {"listId": lst["listId"],
                                                           "name": lst["name"]})
                    print(f"[Tool] project linked -> {board['name']} / {lst['name']} "
                          f"(future pushes need no list)")
            except Exception as e:
                # Never fail the push over this: the cards are already created.
                print(f"[Tool] could not save the project link: {e}")

        print(f"[Tool] push_tasks_to_trello({project_id}) -> {outcome}: {summary}")
        return {
            "outcome": outcome,
            "summary": summary,
            # Kept for callers that only ask "did everything work". Partial
            # counts as NOT fully successful, but `outcome` says why.
            "success": outcome in ("complete", "noop", "needs_review"),
            # WHICH board these cards went to, by name. The planner often
            # answers the next message without calling any tool at all -- it
            # only sees the previous turn's results. Asked right afterwards to
            # "create a Trello board for this project", it proposed one and
            # asked the user to confirm, because nothing it could see said the
            # project already had one.
            "board": _project_link(project_id) or None,
            "created_count": len(created),
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "remote_changed_count": len(remote_changes),
            "conflict_count": len(conflicts),
            "failed_count": len(failed),
            # Edited in Trello since the last export. NOT overwritten.
            "remote_changed": remote_changes,
            # Edited on BOTH sides. Nothing was written for these.
            "conflicts": conflicts,
            "instruction": (
                "Report conflicts and cards changed in Trello EXPLICITLY, naming "
                "the task and which field moved. Nothing was written for them. "
                "Do not offer to overwrite Trello unless the user asks."
                if (conflicts or remote_changes) else ""),
            "created": created,
            # Cards that already existed and were brought in line with the
            # task. Report these as UPDATED, never as created: telling the
            # user a card was created when it was edited is the same lie in
            # the other direction.
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
            "target_list": list_id,
        }
    except Exception as e:
        return {"error": _svc_error(e)}
