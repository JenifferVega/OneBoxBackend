"""EXECUTOR: runs the plan step by step (no LLM).

In debug mode (debug_mode=True) each tool is simulated without touching DynamoDB.
The multi-tenant contextvars (set_current_user) are set by the caller; the graph
runs synchronously on the same stack, so _current_uid() resolves correctly.
execute_tool never raises (returns {"error": ...}); the try/except is a backstop.
"""
import json
import uuid

from agent.graph.nodes.executor.resolve import resolve_params, _get_path
from agent.graph.nodes.executor.validators import validate_tool_params
from agent.graph.state import AgentState
from agent.tools import execute_tool

# ── In-memory cache for projects created in dry-run ─────────────────────────
# Keyed by session_id. Persists while the server is running.
# Cleared with clear_dry_run_cache(session_id) when onebox_reset is called.
_DRY_RUN_PROJECT_CACHE: dict[str, list] = {}
# Keyed by session_id → {projectId: [participants]}
_DRY_RUN_PARTICIPANTS_CACHE: dict[str, dict] = {}


def get_dry_run_projects(session_id: str) -> list:
    return _DRY_RUN_PROJECT_CACHE.get(session_id, [])


def add_dry_run_project(session_id: str, project: dict) -> None:
    if session_id not in _DRY_RUN_PROJECT_CACHE:
        _DRY_RUN_PROJECT_CACHE[session_id] = []
    _DRY_RUN_PROJECT_CACHE[session_id].append(project)


def add_dry_run_participants(session_id: str, project_id: str, participants: list) -> None:
    if session_id not in _DRY_RUN_PARTICIPANTS_CACHE:
        _DRY_RUN_PARTICIPANTS_CACHE[session_id] = {}
    _DRY_RUN_PARTICIPANTS_CACHE[session_id][project_id] = participants


def get_dry_run_participants(session_id: str, project_id: str) -> list:
    return _DRY_RUN_PARTICIPANTS_CACHE.get(session_id, {}).get(project_id, [])


# Trello links created during a dry-run conversation.
# Needed because "link once, then it remembers" is a SEQUENCE: the push has to
# see what a previous turn linked. A stateless fixture cannot model that, and
# the whole point of the feature would go untested.
_DRY_RUN_TRELLO_LINKS: dict = {}

# Which tasks have already reached Trello in this dry-run conversation, and
# which project has already burned its one simulated 429.
# A STATELESS push fixture always reported the same "1 failed", so "retry the
# one that failed" could never be seen to succeed -- the retry path was
# untestable and an agent could loop on it forever.
_DRY_RUN_PUSHED: dict = {}
_DRY_RUN_RATE_LIMITED: dict = {}

# Every board created in this dry-run conversation, so that a SECOND creation
# of the same board is visible instead of silently looking like the first.
_DRY_RUN_BOARDS_CREATED: dict = {}

# The tasks of each fake project. ONE table, used by BOTH list_tasks and
# push_tasks_to_trello: when the push had its own hardcoded list, it reported
# Farmacia Haussman's tasks for whatever project you pushed, and contradicted
# what list_tasks had just said. Incoherent fixtures produce bug reports about
# bugs that do not exist.
_DRY_RUN_TASKS = {
    "proj-DRYRUN-aaa111": [
        ("task-DRYRUN-a1", "Analizar el proceso actual de formulacion magistral", "done",    "Ana Torres",   "2026-07-20"),
        ("task-DRYRUN-a2", "Evaluar la viabilidad tecnica de la automatizacion",  "pending", "Carlos Lopez", "2026-07-30"),
        ("task-DRYRUN-a3", "Definir la arquitectura y el stack tecnologico",      "blocked", "Ana Torres",   "2026-08-05"),
        ("task-DRYRUN-a4", "Desarrollar una solucion de automatizacion",          "pending", "Carlos Lopez", "2026-08-20"),
    ],
    "proj-DRYRUN-bbb222": [
        ("task-DRYRUN-b1", "Migrar el backend a FastAPI",            "done",    "Jesus Vega",   "2026-06-15"),
        ("task-DRYRUN-b2", "Configurar CI en GitHub Actions",        "pending", "Carlos Lopez", "2026-09-01"),
        ("task-DRYRUN-b3", "Documentar el API publico",              "pending", "Ana Torres",   "2026-09-10"),
    ],
    "proj-DRYRUN-ccc333": [
        ("task-DRYRUN-c1", "Disenar la landing page de campana",     "blocked", "Ana Torres",   "2026-07-30"),
        ("task-DRYRUN-c2", "Integrar la pasarela de pago",           "pending", "Carlos Lopez", "2026-08-05"),
        ("task-DRYRUN-c3", "Preparar el calendario de contenidos",   "pending", "Ana Torres",   "2026-08-12"),
    ],
}


def clear_dry_run_cache(session_id: str) -> None:
    _DRY_RUN_PROJECT_CACHE.pop(session_id, None)
    _DRY_RUN_PARTICIPANTS_CACHE.pop(session_id, None)
    _DRY_RUN_TRELLO_LINKS.pop(session_id, None)
    _DRY_RUN_PUSHED.pop(session_id, None)
    _DRY_RUN_RATE_LIMITED.pop(session_id, None)
    _DRY_RUN_BOARDS_CREATED.pop(session_id, None)


def _dry_run_resolve(query: str, session_id: str = "") -> dict:
    """Resolve fake projects using the REAL scorer and the REAL thresholds.

    This used to reimplement the matching with difflib and a 0.75 cutoff --
    a different algorithm and a different gate from production. It therefore
    could not have caught the bug it was supposed to guard: a project that
    does not exist scoring 0.4, coming back as the only candidate, and having
    its id chained into the next step.

    Only the DATA is fake here. Every decision -- the score, the noise floor,
    and whether a single strong candidate earns a machine-usable id -- is
    imported from the tool itself, so the dry run and production can no
    longer disagree about what a name resolves to.
    """
    from agent.tools.resolve import (AUTO_CHAIN_FLOOR, MAX_CANDIDATES,
                                     RECALL_FLOOR, _similarity)
    PROJECTS = [
        ("proj-DRYRUN-aaa111", "Farmacia Haussman", "Automation of magistral formulation"),
        ("proj-DRYRUN-bbb222", "Onebox2026", "Infrastructure project"),
        ("proj-DRYRUN-ccc333", "Marketing Q3", "Quarterly campaign"),
    ]
    scored = []
    for pid, name, desc in PROJECTS:
        score = _similarity(query or "", name)
        if score >= RECALL_FLOOR:
            # Mirrors the real tool: whether this project already has a board
            # travels WITH the project, so the planner knows before proposing
            # to create one. Without it here, the dry run would exercise a
            # field production returns and the fixture does not.
            link = _DRY_RUN_TRELLO_LINKS.get(session_id, {}).get(pid)
            scored.append({"type": "project", "name": name, "score": score,
                           "projectId": pid, "description": desc,
                           "status": "active", "participantsCount": 2,
                           "trelloBoard": ({
                               "boardName": link.get("boardName", ""),
                               "listName": link.get("listName", ""),
                               "note": "This project ALREADY has a Trello "
                                       "board. Do not propose creating "
                                       "another one; a project has ONE "
                                       "board. To send tasks, call "
                                       "push_tasks_to_trello with no board "
                                       "or list.",
                           } if link else None)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    dropped = len(PROJECTS) - len(scored)
    if len(scored) > MAX_CANDIDATES:
        dropped += len(scored) - MAX_CANDIDATES
        scored = scored[:MAX_CANDIDATES]

    out = {"query": query, "count": len(scored), "candidates": scored,
           "dropped_below_floor": dropped, "_dry_run": True}
    exact = [c for c in scored if c["score"] >= 1.0]
    out["exact"] = exact[0] if len(exact) == 1 else None

    strong = [c for c in scored if c["score"] >= AUTO_CHAIN_FLOOR]
    if len(strong) == 1:
        m = strong[0]
        out.update({"type": "project", "name": m["name"],
                    "projectId": m["projectId"], "projectName": m["name"]})

    if not scored:
        out["confidence"] = "none"
        out["instruction"] = (f"Nothing resembles {query!r}. Say it does not "
                              f"exist. Do NOT offer an unrelated project as "
                              f"if it were a correction.")
    elif not strong:
        out["confidence"] = "low"
        out["instruction"] = (f"NO candidate is close enough to assume. "
                              f"{query!r} most likely does not exist. ASK "
                              f"which one they mean, or say nothing matched. "
                              f"Do NOT claim you corrected the name.")
    elif len(strong) > 1:
        out["confidence"] = "ambiguous"
        out["instruction"] = ("Several candidates are equally plausible. Ask "
                              "which one; do not pick for the user.")
    else:
        out["confidence"] = "high"
    return out


def _dry_run_resolve_person(query: str) -> dict:
    """Resolve against the same fake people get_project_contacts returns.

    A query-blind fixture that matched ANY name was the same defect class as
    the query-blind resolve_entity one: it makes a name that does not exist
    resolve anyway, so a test that is supposed to exercise "not found" never
    reaches it.
    """
    import difflib
    PEOPLE = [
        ("Ana Torres",   "ana@company.com",    "+50494622817", "Coordinator"),
        ("Carlos Lopez", "carlos@company.com", "+50494622818", "Developer"),
        ("Jesus Vega",   "jesus@company.com",  "+50494622819", "Owner"),
    ]
    q = (query or "").strip().lower()
    matches = []
    for name, email, phone, role in PEOPLE:
        n = name.lower()
        if q and (q in n or n in q):
            score, fuzzy = 1.0, False
        else:
            score = round(difflib.SequenceMatcher(None, q, n).ratio(), 3)
            fuzzy = True
            if score < 0.75:
                continue
        matches.append({"name": name, "email": email, "phone": phone,
                        "role": role, "score": score, "fuzzy": fuzzy,
                        "projects": [{"projectId": "proj-DRYRUN-aaa111",
                                      "projectName": "Farmacia Haussman"}]})
    matches.sort(key=lambda m: m["score"], reverse=True)
    out = {"query": query, "count": len(matches), "matches": matches,
           "_dry_run": True}
    if len(matches) == 1:
        m = matches[0]
        out["name"] = m["name"]
        # Withhold contact details for a guessed person -- same rule as the
        # real tool: a fuzzy hit must be confirmed before anyone is contacted.
        if m["fuzzy"]:
            out["needs_confirmation"] = True
        else:
            out.update({"email": m["email"], "phone": m["phone"],
                        "projectId": "proj-DRYRUN-aaa111",
                        "projectName": "Farmacia Haussman"})
    return out


def _dry_run_list_tasks(params: dict) -> dict:
    """Tasks of ONE project, from the same table push_tasks_to_trello uses."""
    pid = params.get("project_id") or ""
    tasks = _DRY_RUN_TASKS.get(pid)
    if tasks is None:
        return {"count": 0, "tasks": [], "project_id": pid,
                "note": "No project with that id.", "_dry_run": True}
    want = (params.get("status") or "").strip().lower()
    out = [{"taskId": t, "text": x, "status": s, "assignedTo": w, "dueDate": d}
           for t, x, s, w, d in tasks if not want or s == want]
    return {"count": len(out), "tasks": out, "project_id": pid, "_dry_run": True}


# Realistic simulated results per tool (dry-run)
_DRY_RUN_RESULTS = {
    "create_project":              lambda p: {"success": True, "projectId": f"proj-DRYRUN-{uuid.uuid4().hex[:8]}", "name": p.get("name", "?")},
    "create_task":                 lambda p: {"success": True, "taskId": f"task-DRYRUN-{uuid.uuid4().hex[:8]}", "text": p.get("text", "?")},
    "create_insight":               lambda p: {"success": True, "insightId": f"ins-DRYRUN-{uuid.uuid4().hex[:8]}", "title": p.get("title", "?")},
    "create_reminder":          lambda p: {"success": True, "reminder_id": f"rem-DRYRUN-{uuid.uuid4().hex[:8]}", "title": p.get("title", "?")},
    "send_email":               lambda p: {"success": True, "email_id": f"email-DRYRUN-{uuid.uuid4().hex[:8]}", "status": "simulated", "recipient": p.get("recipient_email", "?")},
    "send_notification":         lambda p: {"success": True, "sid": f"SM-DRYRUN-{uuid.uuid4().hex[:16]}", "status": "simulated", "channel": p.get("channel", "?"), "recipient": p.get("recipient", "?")},
    "assign_email_to_project":   lambda p: {"success": True, "conversationId": p.get("conversation_id", "?"), "projectId": p.get("project_id", "?")},
    # Names MUST match what resolve_entity returns. They did not, and the two
    # fixtures contradicted each other: resolve_entity resolved
    # proj-DRYRUN-aaa111 to "Farmacia Haussman" while list_projects called the
    # same id "Demo Project A". The agent then reported a project mismatch --
    # a correct reaction to incoherent test data, and a false failure.
    "list_projects":            lambda p: {"count": 3, "projects": [
        {"projectId": "proj-DRYRUN-aaa111", "name": "Farmacia Haussman", "type": "Consulting", "status": "active"},
        {"projectId": "proj-DRYRUN-bbb222", "name": "Onebox2026", "type": "Infrastructure", "status": "active"},
        {"projectId": "proj-DRYRUN-ccc333", "name": "Marketing Q3", "type": "Marketing", "status": "active"},
    ], "_dry_run": True},
    "list_emails":              lambda p: {"count": 2, "emails": [
        {"email_id": "email-DRYRUN-cc1", "subject": "Meeting tomorrow", "from": "team@company.com"},
        {"email_id": "email-DRYRUN-cc2", "subject": "Pending invoice", "from": "vendor@company.com"},
    ], "_dry_run": True},
    "inspect_email":         lambda p: {"email_id": p.get("email_id", "?"), "subject": "[DRY RUN]", "body": "Simulated email content.", "_dry_run": True},
    "analyze_inbox":              lambda p: {"count": 2, "emails": [
        {"email_id": "email-DRYRUN-cc1", "subject": "Meeting tomorrow", "suggested_project": "proj-DRYRUN-aaa111"},
    ], "_dry_run": True},
    "list_notifications":       lambda p: {"count": 1, "notifications": [
        {"id": "notif-DRYRUN-001", "message": "Pending task", "status": "unread"},
    ], "_dry_run": True},
    "get_project_contacts":  lambda p: {"total_contacts": 2, "contacts": [
        {"name": "Ana Torres", "phone": "+50494622817", "role": "Coordinator", "pending_tasks": 2},
        {"name": "Carlos Lopez", "phone": "+50494622818", "role": "Developer", "pending_tasks": 1},
    ], "_dry_run": True},
    "list_tasks":               lambda p: _dry_run_list_tasks(p),
    "update_task":            lambda p: {"success": True, "taskId": p.get("task_id", "?"), "status": "simulated", "_dry_run": True},
    "delete_task":              lambda p: {"success": True, "taskId": p.get("task_id", "?"), "childrenAffected": 0, "cascade": p.get("cascade", False), "_dry_run": True},
    "update_project":         lambda p: {"success": True, "projectId": p.get("project_id", "?"), "updated": [k for k in ("name","description","type","status","delivery_date","timing") if p.get(k) is not None], "_dry_run": True},
    "invite_user":             lambda p: {"success": True, "saved": True, "notified": bool(p.get("send_notification", True)), "email": p.get("email", ""), "phone": p.get("phone", ""), "_dry_run": True},
    "update_participants":    lambda p: {"success": True, "projectId": p.get("project_id", "?"), "_dry_run": True},
    "remove_participant":         lambda p: {"success": True, "removed": {"email": p.get("email", ""), "phone": p.get("phone", ""), "name": p.get("name", "")}, "_dry_run": True},
    "resolve_person":            lambda p: _dry_run_resolve_person(p.get("name", "")),
    "check_sla":               lambda p: {"total_alerts": 0, "alerts": [], "_dry_run": True},
    "auto_classify_messages": lambda p: {"suggestions": [], "_dry_run": True},
    "proactive_summary":           lambda p: {"projects_count": 0, "suggested_actions": [], "_dry_run": True},

    # ── Name resolution ────────────────────────────────────────────────────
    # Returns ONE candidate that is deliberately NOT an exact match
    # ("exact": None, score below 1.0). That is the interesting path: the
    # narrator must disclose that it corrected a typo instead of silently
    # answering as if the user had typed the right name.
    # QUERY-AWARE. It used to return "Farmacia Haussman" for ANY query, so
    # every test resolved to the same project no matter what was asked --
    # including projects that do not exist, which made a "not found" path
    # impossible to test and quietly matched invented names.

    # ── Trello ─────────────────────────────────────────────────────────────
    # Two boards with differently named lists on purpose: the agent must ask
    # WHICH list, and cannot guess a status mapping from the names alone.
    # Board names mirror the user's REAL Trello, and deliberately none of them
    # is named after a project. A fixture board called "Farmacia Haussman"
    # made an invented board_name (derived from the project name) look valid,
    # hiding exactly the mistake this is meant to catch.
    "list_trello_boards":      lambda p: {"count": 2, "boards": [
        {"boardId": "board-DRYRUN-111", "name": "ONEBOX",
         "url": "https://trello.com/b/DRYRUN111",
         "lists": [
             {"listId": "list-DRYRUN-a1", "name": "MVP - TO DO"},
             {"listId": "list-DRYRUN-a2", "name": "Backlog"},
             {"listId": "list-DRYRUN-a3", "name": "Done"},
         ]},
        {"boardId": "board-DRYRUN-222", "name": "Bienvenido a Trello",
         "url": "https://trello.com/b/DRYRUN222",
         "lists": [
             {"listId": "list-DRYRUN-b1", "name": "Por hacer"},
             {"listId": "list-DRYRUN-b2", "name": "En curso"},
             {"listId": "list-DRYRUN-b3", "name": "Hecho"},
         ]},
    ],
        # Must mirror the real tool field for field. Without this the dry run
        # tested a conversation where creating a board was never on offer.
        "also_available": {
            "option": "create_new", "tool": "create_trello_board",
            "how": "Create a NEW board for the project with OneBox's own "
                   "statuses as columns (Pendiente / En curso / Bloqueado / "
                   "Hecho), which keeps list and status in step. REQUIRES the "
                   "user to agree first."},
        "instruction": "When the user has not said where the cards go, offer "
                       "BOTH: one of the boards listed here, or a new board for "
                       "this project. Having boards already does not mean they "
                       "want this project's cards in one.",
        "_dry_run": True},

    # A MIXED result on purpose: something created, something skipped, and one
    # failure. A fixture where everything succeeds cannot catch the narrator
    # reporting a blanket "done" -- which is exactly what it must not do.

}


def _simulate_tool(tool_name: str, params: dict, session_id: str = "") -> dict:
    """Simulate the execution of a tool without touching any database."""
    if tool_name == "list_projects":
        # Same list as _DRY_RUN_RESULTS["list_projects"] -- keep them in sync.
        base = [
            {"projectId": "proj-DRYRUN-aaa111", "name": "Farmacia Haussman", "type": "Consulting",    "status": "active"},
            {"projectId": "proj-DRYRUN-bbb222", "name": "Onebox2026",        "type": "Infrastructure", "status": "active"},
            {"projectId": "proj-DRYRUN-ccc333", "name": "Marketing Q3",      "type": "Marketing",      "status": "active"},
        ]
        extra = get_dry_run_projects(session_id) if session_id else []
        all_projects = base + extra
        return {"count": len(all_projects), "projects": all_projects, "_dry_run": True}

    if tool_name == "get_project_contacts":
        project_id = params.get("project_id", "")
        # Try by exact project_id first, then fall back to the session "latest"
        cached = get_dry_run_participants(session_id, project_id) if session_id else []
        if not cached and session_id:
            cached = get_dry_run_participants(session_id, "_latest")
        if cached:
            return {"total_contacts": len(cached), "contacts": cached, "_dry_run": True}
        return {"total_contacts": 0, "contacts": [], "_dry_run": True,
                "_note": "No participants registered for this project in dry-run"}

    # resolve_entity needs the session: whether a project already has a
    # Trello board is per-conversation state in the dry run.
    if tool_name == "resolve_entity":
        return _dry_run_resolve(params.get("query", ""), session_id)

    if tool_name == "prepare_trello_board":
        # Mirrors the real tool: resolve the project AND report its link in
        # one answer, so the dry run exercises the same single-step shape.
        found = _dry_run_resolve(params.get("project_query", ""), session_id)
        pid = found.get("projectId")
        if not pid:
            return {"resolved": False, "can_create": False,
                    "query": params.get("project_query", ""),
                    "confidence": found.get("confidence", "none"),
                    "candidates": found.get("candidates", []),
                    "instruction": found.get(
                        "instruction", "The project could not be identified. "
                                       "Ask which one they mean."),
                    "_dry_run": True}
        link = _DRY_RUN_TRELLO_LINKS.get(session_id, {}).get(pid)
        out = {"resolved": True, "projectId": pid,
               "projectName": found.get("projectName", ""),
               "confidence": found.get("confidence", "high"), "_dry_run": True}
        if link:
            out.update({
                "can_create": False, "already_linked": link,
                "instruction": (
                    f"This project is ALREADY linked to the Trello board "
                    f"'{link.get('boardName')}' (list '{link.get('listName')}'). "
                    f"A project has ONE board. Tell the user which board it is "
                    f"and offer to send the tasks there with "
                    f"push_tasks_to_trello. Do NOT propose creating another "
                    f"board and do NOT ask them to confirm creating one.")})
        else:
            open_tasks = [t for t in _DRY_RUN_TASKS.get(pid, [])
                          if t[2] != "done"]
            out.update({
                "can_create": True, "already_linked": None,
                "suggested_name": found.get("projectName", ""),
                "suggested_lists": ["Pendiente", "En curso", "Bloqueado", "Hecho"],
                "next_tool": "create_trello_board",
                "tasks_waiting_to_export": len(open_tasks),
                "instruction": (
                    "This project has NO Trello board. Creating one writes into "
                    "the user's Trello account, so PROPOSE it and ask for "
                    "confirmation first.")})
        return out

    # ── Trello: these three share state across turns ───────────────────────
    if tool_name == "create_trello_board":
        # Mirrors the real tool: a project that already has a board does not
        # silently get a second one.
        pid_pre = (params.get("project_id") or "").strip()
        existing = _DRY_RUN_TRELLO_LINKS.get(session_id, {}).get(pid_pre)
        if pid_pre and existing:
            return {
                "error": f"This project is already linked to the Trello board "
                         f"'{existing.get('boardName')}' (list "
                         f"'{existing.get('listName')}'). Creating another board "
                         f"would leave two boards for one project.",
                "already_linked": existing, "needs_user_choice": True,
                "options": [
                    {"option": "push_to_existing",
                     "how": "Send the cards to the board it is already linked "
                            "to: call push_tasks_to_trello with no list_name "
                            "and no board_name.",
                     "tool": "push_tasks_to_trello"},
                    {"option": "relink",
                     "how": "Point the project at a DIFFERENT existing board or "
                            "list instead.",
                     "tool": "link_project_to_trello"},
                ],
                "instruction": "Do NOT create a second board on your own. Say "
                               "which board it is already linked to and ask "
                               "what they want.",
                "_dry_run": True}

        lists = [l for l in (params.get("lists") or
                 ["Pendiente", "En curso", "Bloqueado", "Hecho"]) if l]
        # A FIXED board id made a second creation invisible: two boards in the
        # user's real Trello account would look exactly like one. The validator
        # can order a replan of any tool, writes included, so a duplicate
        # create has to be visible in the result or it cannot be caught here.
        made = _DRY_RUN_BOARDS_CREATED.setdefault(session_id, [])
        n = len(made) + 1
        board = {"boardId": f"board-DRYRUN-{n}", "name": params.get("name", "?"),
                 "url": f"https://trello.com/b/DRYRUNNEW{n}",
                 "lists": [{"listId": f"list-DRYRUN-n{n}{i}", "name": nm}
                           for i, nm in enumerate(lists, 1)]}
        made.append({"boardId": board["boardId"], "name": board["name"],
                     "project_id": params.get("project_id")})
        dupes = [b for b in made if b["name"] == board["name"]]
        if len(dupes) > 1:
            board["_duplicate_warning"] = (
                f"{len(dupes)} boards named '{board['name']}' have been created "
                f"in this conversation. In production these are REAL boards in "
                f"the user's Trello account.")
            print(f"   [DRY-RUN] ⚠️  DUPLICATE create_trello_board: "
                  f"'{board['name']}' created {len(dupes)} times this session.")
        board["_boards_created_this_session"] = len(made)
        linked = None
        pid = params.get("project_id")
        if pid and board["lists"]:
            first = board["lists"][0]
            _DRY_RUN_TRELLO_LINKS.setdefault(session_id, {})[pid] = {
                "boardName": board["name"], "listName": first["name"],
                "listId": first["listId"]}
            linked = {"projectId": pid, "listId": first["listId"],
                      "listName": first["name"]}
        return {"success": True, **board, "linked": linked, "_dry_run": True}

    if tool_name == "link_project_to_trello":
        pid = params.get("project_id", "?")
        link = {"boardName": params.get("board_name") or "ONEBOX",
                "listName": params.get("list_name") or "MVP - TO DO",
                "listId": "list-DRYRUN-a1"}
        _DRY_RUN_TRELLO_LINKS.setdefault(session_id, {})[pid] = link
        return {"success": True, "projectId": pid, **link, "_dry_run": True}

    if tool_name == "push_tasks_to_trello":
        pid = params.get("project_id", "")
        link = _DRY_RUN_TRELLO_LINKS.get(session_id, {}).get(pid)
        if not (params.get("list_id") or params.get("list_name") or link):
            # Byte-for-byte the shape the real tool returns, or the dry-run
            # tests a different conversation than production has.
            return {
                "error": "This project has no Trello list yet and none was "
                         "given. Ask the user which of TWO options they want.",
                "needs_user_choice": True,
                "options": [
                    {"option": "use_existing",
                     "how": "They pick one of their boards and a list on it; "
                            "then call push_tasks_to_trello with list_name and "
                            "board_name.",
                     "tool": "push_tasks_to_trello"},
                    {"option": "create_new",
                     "how": "Create a board for this project with OneBox's own "
                            "statuses as columns (Pendiente / En curso / "
                            "Bloqueado / Hecho). REQUIRES the user to agree "
                            "first.",
                     "tool": "create_trello_board"},
                ],
                "instruction": "Offer BOTH options. Having boards already does "
                               "not mean they want this project's cards in one "
                               "of them.",
                "_dry_run": True}
        # Validate the names against the fake boards, exactly as the real tool
        # validates them against Trello. Without this the fixture accepted
        # ANY board_name/list_name, so a planner that INVENTED them looked
        # like it had worked -- the harness green-lighting behaviour that
        # production rejects.
        FAKE = {"onebox": ["mvp - to do", "backlog", "done"],
                "bienvenido a trello": ["por hacer", "en curso", "hecho"]}
        bn = (params.get("board_name") or "").strip().lower()
        ln = (params.get("list_name") or "").strip().lower()
        if bn and bn not in FAKE:
            return {"error": f"No list matching '{params.get('list_name')}' on a "
                             f"board matching '{params.get('board_name')}'. "
                             f"Available boards: ONEBOX, Bienvenido a Trello",
                    "_dry_run": True}
        if ln and not any(ln in l or l in ln
                          for l in (FAKE.get(bn) or sum(FAKE.values(), []))):
            return {"error": f"No list matching '{params.get('list_name')}'. "
                             f"Available lists: MVP - TO DO; Backlog; Done; "
                             f"Por hacer; En curso; Hecho", "_dry_run": True}

        where = (params.get("list_name") or (link or {}).get("listName") or "the list")

        tasks = _DRY_RUN_TASKS.get(pid, [])
        already = _DRY_RUN_PUSHED.setdefault(session_id, {}).setdefault(pid, set())
        burned = _DRY_RUN_RATE_LIMITED.setdefault(session_id, set())

        created, skipped, failed = [], [], []
        for tid, text, status, _who, _due in tasks:
            if status == "done":
                continue                      # the real tool pushes open tasks
            if tid in already:
                skipped.append({"text": text, "reason": "already in Trello",
                                "cardId": f"card-DRYRUN-{tid[-2:]}"})
                continue
            # The LAST open task hits a 429 the FIRST time this project is
            # pushed, and succeeds on the retry. That is what makes "retry the
            # one that failed" a testable path instead of an infinite loop.
            is_last = tid == [t[0] for t in tasks if t[2] != "done"][-1]
            if is_last and pid not in burned:
                burned.add(pid)
                failed.append({"text": text, "taskId": tid,
                               "error": "Trello card creation failed (429): "
                                        "rate limit exceeded"})
                continue
            already.add(tid)
            created.append({"text": text, "taskId": tid,
                            "cardId": f"card-DRYRUN-{tid[-2:]}",
                            "url": f"https://trello.com/c/DRYRUN{tid[-2:]}"})

        if not tasks:
            outcome = "noop"
        elif failed and created:
            outcome = "partial"
        elif failed:
            outcome = "failed"
        elif created:
            outcome = "complete"
        else:
            outcome = "noop"

        return {
            "outcome": outcome,
            "summary": (f"{len(created)} created, {len(skipped)} skipped, "
                        f"{len(failed)} failed"),
            # True when something reached Trello OR nothing went wrong.
            # Two traps here, both seen for real: reporting a PARTIAL push as a
            # failure made the narrator mention only the error while real cards
            # existed; and reporting "everything was already there" as a failure
            # invents a problem the user does not have.
            "success": bool(created) or not failed,
            # WHICH board, by name -- the planner often answers the next
            # message from previous_results alone, with no tool call.
            "board": (link or _DRY_RUN_TRELLO_LINKS.get(session_id, {}).get(pid)),
            "target_list": where,
            "created_count": len(created), "skipped_count": len(skipped),
            "failed_count": len(failed),
            "created": created, "skipped": skipped, "failed": failed,
            "_dry_run": True}

    sim = _DRY_RUN_RESULTS.get(tool_name)
    if sim:
        return sim(params)

    # NO FIXTURE. This is not a harmless default: the empty result makes the
    # validator judge the plan incomplete, the planner replans the same thing,
    # and the turn dies after three iterations with no usable answer. It went
    # unnoticed for a month on resolve_entity because the only symptom is a
    # conversation that quietly goes nowhere. Say it out loud instead.
    print(f"   [DRY-RUN] ⚠️  No fixture for '{tool_name}'. Debug conversations "
          f"using it WILL burn every iteration and end with no data. "
          f"Add it to _DRY_RUN_RESULTS in this file.")
    return {"_dry_run": True, "_no_fixture": True, "tool": tool_name,
            "params": params,
            "_note": f"No dry-run fixture defined for '{tool_name}'. This is a "
                     f"test-harness gap, NOT a failure of the tool itself."}


def _execute_foreach_step(
    tool_name: str,
    step_num: int,
    params: dict,
    foreach_key: str,
    foreach_spec: dict,
    results: dict,
    debug_mode: bool,
    session_id: str = "",
) -> dict:
    """Expand a step with foreach: run the tool once per item in the list.

    foreach_spec example:
      {"from_step": 2, "foreach": "contacts", "extract": "phone"}

    Returns a consolidated dict with:
      - sent: list of {name, <extract_field>, result}
      - skipped: list of {name, reason}
      - sent_count / skipped_count
    """
    step_ref = foreach_spec["from_step"]
    source = results.get(step_ref, {})
    foreach_path = foreach_spec["foreach"]        # e.g. "contacts"
    extract_field = foreach_spec.get("extract", "phone")
    name_field = foreach_spec.get("name_field", "name")

    # Get the list; supports dot-notation ("a.b") or a direct key
    items_list = _get_path(source, foreach_path) if "." in foreach_path else (
        source.get(foreach_path, []) if isinstance(source, dict) else []
    )

    if not items_list:
        print(f"   foreach: list '{foreach_path}' is empty or not found")
        return {"sent_count": 0, "skipped_count": 0, "sent": [], "skipped": [], "_foreach_result": True}

    # Resolve the rest of the parameters (the non-foreach ones) once
    other_params = {k: v for k, v in params.items() if k != foreach_key}
    other_resolved = resolve_params(other_params, results)

    # Relative/absolute scheduling → scheduled_at/recurring_days (deterministic).
    # Applies to the bulk send (same scheduled_at for all recipients).
    if isinstance(other_resolved.get("schedule"), dict):
        from agent.schedule import apply_schedule
        other_resolved, _serr = apply_schedule(other_resolved)
        if _serr:
            return {"error": _serr.get("error"), "suggestion": _serr.get("suggestion"),
                    "_schedule_error": True, "sent_count": 0, "skipped_count": 0,
                    "sent": [], "skipped": [], "_foreach_result": True}

    sent = []
    skipped = []

    for item in items_list:
        name = item.get(name_field, "Unknown contact") if isinstance(item, dict) else str(item)
        value = item.get(extract_field) if isinstance(item, dict) else None

        if not value or not isinstance(value, str) or not value.strip():
            reason = f"No '{extract_field}' on record"
            skipped.append({"name": name, "reason": reason})
            print(f"   Skipping {name}: {reason}")
            continue

        iter_params = {**other_resolved, foreach_key: value}
        print(f"   → {name} ({value})")

        if debug_mode:
            iter_result = _simulate_tool(tool_name, iter_params, session_id)
        else:
            iter_result = execute_tool(tool_name, iter_params)

        sent.append({"name": name, extract_field: value, "result": iter_result})
        print(f"   ok {json.dumps(iter_result, default=str)[:100]}")

    return {
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "sent": sent,
        "skipped": skipped,
        "_foreach_result": True,
    }


# ── Which tools have side effects ───────────────────────────────────────────
# A replan re-executes the plan FROM STEP 1. For a read that is merely
# wasteful; for a write it means doing it twice. Observed for real: one turn
# created TWO identical Trello boards because a 429 on the LAST step sent the
# whole plan round again.
_WRITE_TOOLS = {
    "send_email", "send_notification",
    "create_project", "update_project", "assign_email_to_project",
    "invite_user", "update_participants", "remove_participant",
    "create_task", "update_task", "delete_task", "create_reminder",
    "create_insight",
    "create_trello_board", "link_project_to_trello", "push_tasks_to_trello",
}
_READ_TOOLS = {
    "list_emails", "inspect_email", "analyze_inbox",
    "list_projects", "get_project_contacts", "list_tasks",
    "list_notifications", "check_sla", "auto_classify_messages",
    "proactive_summary", "resolve_entity", "resolve_person",
    "list_trello_boards",
    # Read-only on purpose: it answers "can a board be created here?"
    # without creating anything.
    "prepare_trello_board",
}


def _audit_tool_classification() -> None:
    """Every registered tool must be classified as a read or a write.

    An unclassified tool is treated as a read, which means it can be
    re-executed by a replan. That is the WRONG default for anything that
    writes, so a new tool must not be able to slip in silently.
    """
    try:
        from agent.tools import TOOL_MAP
    except Exception:
        return
    unknown = set(TOOL_MAP) - _WRITE_TOOLS - _READ_TOOLS
    if unknown:
        print(f"   [EXECUTOR] ⚠️  Unclassified tools: {sorted(unknown)}. "
              f"Add each to _WRITE_TOOLS or _READ_TOOLS in this file. Until "
              f"then they are treated as reads and a replan MAY RUN THEM "
              f"TWICE -- which for a write means doing it twice for real.")


_audit_tool_classification()


def _write_fingerprint(tool_name: str, params: dict) -> str:
    """Identity of a write: the tool plus the parameters it actually ran with.

    Uses RESOLVED parameters, so the same call still matches when a replan
    expresses it differently (a literal project_id one time, a from_step
    reference the next).
    """
    try:
        body = json.dumps(params, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        body = repr(sorted(params.items())) if isinstance(params, dict) else repr(params)
    return f"{tool_name}::{body}"


def _write_succeeded(result) -> bool:
    """Did this write take effect? If so it must never be repeated.

    A write that failed OUTRIGHT is allowed to run again -- that is the retry
    path. The gap this cannot close is a write that errored AFTER taking
    effect (a timeout on the response, say); closing that needs idempotency
    keys in the tools themselves.
    """
    if not isinstance(result, dict):
        return bool(result)
    if result.get("_validation_error") or result.get("_schedule_error"):
        return False
    if result.get("error"):
        return False
    if result.get("success") is False:
        return False
    return True


def executor_node(state: AgentState) -> dict:
    print("\n" + "=" * 60)
    print("EXECUTOR")
    print("=" * 60)

    plan = state.get("plan", [])
    results = dict(state.get("results", {}))
    tools_used = list(state.get("tools_used", []))
    debug_mode = state.get("debug_mode", False)
    session_id = state.get("session_id") or ""
    simulated_calls = []
    # Writes already performed in this turn. Empty on the first iteration;
    # on a replan it holds what the earlier attempt actually did.
    executed_writes = dict(state.get("executed_writes", {}))

    if debug_mode:
        print("   DEBUG MODE — dry-run active, the database will not be modified")

    if not plan:
        print("   No plan to execute")
        return {"status": "validating"}

    for step in plan:
        step_num = step.get("step", 0)
        tool_name = step.get("tool", "")
        params = step.get("params", {})

        print(f"\n   Step {step_num}: {tool_name}")
        try:
            # ── Detect foreach in any param ─────────────────────────
            foreach_key = next(
                (k for k, v in params.items() if isinstance(v, dict) and "foreach" in v and "from_step" in v),
                None,
            )

            # ── Has this exact write already happened in this turn? ──
            # A foreach step is fingerprinted on its RAW params: the per-item
            # values are resolved inside the helper, but the spec that
            # produces them is stable within the turn. A foreach is usually
            # send_notification over every contact, so repeating it means
            # messaging the whole team twice.
            # Deliberately fingerprinted BEFORE apply_schedule: that derives
            # scheduled_at from the clock, so the same send would fingerprint
            # differently on a replan seconds later and be sent twice.
            reused, fingerprint = None, ""
            if tool_name in _WRITE_TOOLS:
                fingerprint = _write_fingerprint(
                    tool_name,
                    params if foreach_key else resolve_params(params, results),
                )
                reused = executed_writes.get(fingerprint)

            if reused is not None:
                print(f"   [LEDGER] '{tool_name}' already succeeded in this turn "
                      f"(step {reused['step']}). Reusing that result instead of "
                      f"performing the write a second time.")
                result = reused["result"]
                resolved_params = reused.get("params", params)

            elif foreach_key:
                # Loop execution: one tool call per item in the list
                print(f"   [FOREACH] expanding by '{params[foreach_key]['foreach']}'")
                result = _execute_foreach_step(
                    tool_name, step_num, params,
                    foreach_key, params[foreach_key],
                    results, debug_mode,
                    session_id=session_id,
                )
                resolved_params = params  # for the simulated_calls log
            else:
                resolved_params = resolve_params(params, results)

                # ── Deterministic scheduling (send_notification only) ─────
                # The planner emits normalized resolved_params["schedule"]
                # ({"type": "relative"/"fixed_time"/"next_day"/"recurring", ...}).
                # Here (code, not LLM) it is converted to scheduled_at UTC or
                # recurring_days using the server's actual clock.
                sched_error = None
                if tool_name == "send_notification" and isinstance(resolved_params.get("schedule"), dict):
                    from agent.schedule import apply_schedule
                    resolved_params, sched_error = apply_schedule(resolved_params)

                print(f"   Params: {json.dumps(resolved_params, default=str)[:200]}")

                # ── Parameter validation before executing ──────────────
                validation_error = validate_tool_params(tool_name, step_num, resolved_params)
                if sched_error:
                    print(f"   INVALID SCHEDULE: {sched_error.get('error')}")
                    result = {"error": sched_error.get("error"),
                              "suggestion": sched_error.get("suggestion"),
                              "_schedule_error": True}
                elif validation_error:
                    print(f"   VALIDATION FAILED: {validation_error['error'][:150]}")
                    result = validation_error
                elif debug_mode:
                    result = _simulate_tool(tool_name, resolved_params, session_id)
                    print(f"   [DRY RUN] Simulated result: {json.dumps(result, default=str)[:200]}")
                    # Save into in-memory cache so list_projects sees it in later turns
                    if tool_name == "create_project" and result.get("success") and session_id:
                        project_id = result.get("projectId", f"proj-DRYRUN-{uuid.uuid4().hex[:8]}")
                        add_dry_run_project(session_id, {
                            "projectId": project_id,
                            "name":      resolved_params.get("name", "?"),
                            "type":      resolved_params.get("type", "Other"),
                            "status":    "active",
                        })
                        # Save participants so get_project_contacts returns them
                        raw_participants = resolved_params.get("participants", [])
                        if raw_participants:
                            contacts = [
                                {
                                    "name":           p.get("name", "?"),
                                    "role":              p.get("role", ""),
                                    "phone":         p.get("phone", ""),
                                    "email":            p.get("email", ""),
                                    "pending_tasks": 0,
                                }
                                for p in raw_participants if isinstance(p, dict)
                            ]
                            add_dry_run_participants(session_id, project_id, contacts)
                            # "_latest" fallback so lookups still work if project_id doesn't match
                            add_dry_run_participants(session_id, "_latest", contacts)
                        print(f"   [DRY RUN] Project saved to cache: {resolved_params.get('name')}")
                else:
                    result = execute_tool(tool_name, resolved_params)

        except Exception as e:
            print(f"   Unexpected error in step: {e}")
            result = {"error": str(e)}
            resolved_params = params
            fingerprint, reused = "", None

        # Record the write so a replan does not repeat it. A write that failed
        # OUTRIGHT is deliberately NOT recorded: re-running it is the retry
        # path, and a failed write most likely changed nothing.
        if fingerprint and reused is None and _write_succeeded(result):
            executed_writes[fingerprint] = {
                "step": step_num, "tool": tool_name,
                "params": resolved_params, "result": result,
            }

        results[step_num] = result
        tools_used.append(tool_name)

        if debug_mode:
            simulated_calls.append({
                "step": step_num,
                "tool": tool_name,
                "params": resolved_params,
                "simulated_result": result,
            })

        preview = json.dumps(result, default=str, ensure_ascii=False)[:200]
        print(f"   Result: {preview}...")

    print(f"\n   Plan {'simulated' if debug_mode else 'executed'}: {len(plan)} steps")

    update = {"results": results, "tools_used": tools_used,
              "executed_writes": executed_writes, "status": "validating"}
    if debug_mode and simulated_calls:
        existing = state.get("debug_info") or {}
        update["debug_info"] = {**existing, "simulated_calls": simulated_calls}
    return update
