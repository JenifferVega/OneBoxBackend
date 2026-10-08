"""Who may change what, and what reaches Trello -- on the REAL services.

    python tests/test_member_rules_and_trello_sync.py

No AWS, no Trello, no LLM: DynamoDB tables are in-memory fakes and every
Trello call is answered by a small fake board that records what was written.

  1. member permissions on tasks (own task, status only, comment required)
  2. proposal decisions are the owner's
  3. a task change in OneBox reaches the board (owner's connection)
  4. a card moved to another column is NOT treated as deleted on re-export
  5. board invitations: log, retry, and "they got an email later"
"""
import copy
import os
import re
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRELLO_API_KEY", "test-key")

from fastapi import HTTPException  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   <- {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def status_of(fn, *a, **k):
    """HTTP status a service call ends with (200 when it does not raise)."""
    try:
        fn(*a, **k)
        return 200
    except HTTPException as e:
        return e.status_code


# ── a fake DynamoDB table ───────────────────────────────────────────────────

def _match(cond, item):
    e = cond.get_expression()
    op, v = e["operator"], e["values"]
    if op == "AND":
        return _match(v[0], item) and _match(v[1], item)
    if op == "=":
        return item.get(v[0].name) == v[1]
    raise NotImplementedError(op)


class Table:
    def __init__(self, key, items=()):
        self.key, self.items = key, {}
        for it in items:
            self.put_item(Item=it)

    def put_item(self, Item):
        self.items[Item[self.key]] = copy.deepcopy(Item)

    def get_item(self, Key):
        k = Key[self.key]
        return {"Item": copy.deepcopy(self.items[k])} if k in self.items else {}

    def delete_item(self, Key):
        self.items.pop(Key[self.key], None)

    def scan(self, FilterExpression=None, **_kw):
        out = [copy.deepcopy(v) for v in self.items.values()
               if FilterExpression is None or _match(FilterExpression, v)]
        return {"Items": out}

    def query(self, **_kw):
        return {"Items": []}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **_kw):
        vals, names = ExpressionAttributeValues or {}, ExpressionAttributeNames or {}
        it = self.items.setdefault(Key[self.key], dict(Key))
        expr = UpdateExpression.strip()
        if expr.upper().startswith("REMOVE "):
            for f in expr[7:].split(","):
                it.pop(f.strip(), None)
            return
        assert expr.upper().startswith("SET "), expr
        body = expr[4:]
        m = re.fullmatch(r"(\w+) = list_append\(if_not_exists\(\w+, (:\w+)\), (:\w+)\)", body)
        if m:
            it[m.group(1)] = list(it.get(m.group(1)) or vals[m.group(2)]) + copy.deepcopy(vals[m.group(3)])
            return
        for part in body.split(","):
            lhs, rhs = [x.strip() for x in part.split("=")]
            it[names.get(lhs, lhs)] = copy.deepcopy(vals[rhs])


# ── a fake Trello board ─────────────────────────────────────────────────────

class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)[:200]

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


BOARD, L_HOME, L_BLOCK, L_DONE = "B1", "L1", "L3", "L4"


class Trello:
    def __init__(self):
        self.lists = [{"id": L_HOME, "name": "Pendiente"}, {"id": "L2", "name": "En curso"},
                      {"id": L_BLOCK, "name": "Bloqueado"}, {"id": L_DONE, "name": "Hecho"}]
        self.cards, self.writes, self.reject, self.n = {}, [], set(), 0

    def request(self, method, url, params=None, **_kw):
        path = urlparse(url).path
        p = {k: v for k, v in (params or {}).items() if k not in ("key", "token")}
        if method == "GET":
            if path == f"/1/boards/{BOARD}/lists":
                return Resp([dict(l, closed=False) for l in self.lists])
            m = re.fullmatch(r"/1/lists/(\w+)/cards", path)
            if m:   # like Trello: archived cards are not listed
                return Resp([c for c in self.cards.values()
                             if c["idList"] == m.group(1) and not c["closed"]])
            return Resp({"error": "unknown"}, 404)
        self.writes.append({"method": method, "path": path, "params": p})
        if method == "POST" and path == "/1/cards":
            self.n += 1
            cid = f"card{self.n}"
            self.cards[cid] = {"id": cid, "name": p["name"], "desc": p.get("desc", ""),
                               "due": p.get("due"), "idList": p["idList"], "closed": False,
                               "dateLastActivity": "", "shortUrl": f"https://trello.com/c/{cid}"}
            return Resp(self.cards[cid])
        m = re.fullmatch(r"/1/cards/(\w+)", path)
        if method == "PUT" and m:
            c = self.cards.get(m.group(1))
            if not c:
                return Resp({"error": "gone"}, 404)
            for k in ("name", "desc", "due", "idList"):
                if k in p:
                    c[k] = p[k]
            if p.get("closed") == "true":
                c["closed"] = True
            return Resp(c)
        if method == "POST" and re.fullmatch(r"/1/cards/\w+/actions/comments", path):
            return Resp({"id": "comment"})
        if method == "PUT" and path == f"/1/boards/{BOARD}/members":
            return Resp({"error": "invite refused"}, 403) if p["email"] in self.reject else Resp({"id": BOARD})
        return Resp({"error": "unknown"}, 404)

    def did(self, method, path_re, **params):
        return [w for w in self.writes if w["method"] == method and re.fullmatch(path_re, w["path"])
                and all(w["params"].get(k) == v for k, v in params.items())]


# ── the world ───────────────────────────────────────────────────────────────

OWNER, MEMBER, OTHER = "uid-owner", "uid-ana", "uid-beto"
E_OWNER, E_ANA, E_BETO = "santi@x.co", "ana@x.co", "beto@x.co"


def world(linked=True):
    import agent.tools as tools
    import agent.tools.trello as tool_trello
    import api.services.access as access
    import api.services.checkin as checkin
    import api.services.planning_chat as PC
    import api.services.projects as projects
    import api.services.tasks as tasks
    import api.services.trello as TS
    import api.services.trello_sync as sync

    proj = {"projectId": "p1", "name": "Demo", "userId": OWNER, "participants": [
        {"name": "Santi", "email": E_OWNER}, {"name": "Ana Ruiz", "email": E_ANA},
        {"name": "Beto Paz", "email": E_BETO}, {"name": "Carla", "email": ""}]}
    if linked:
        proj.update(trelloBoardId=BOARD, trelloBoardName="Demo", trelloListId=L_HOME,
                    trelloListName="Pendiente")
    P, T, I = Table("projectId", [proj]), Table("taskId"), Table("invitationId")
    for mod in (tools, tasks, access, tool_trello, projects):
        for name, tbl in (("projects_table", P), ("tasks_table", T), ("invitations_table", I)):
            if hasattr(mod, name):
                setattr(mod, name, tbl)
    PC._table = Table("itemId")

    tr = Trello()
    TS.TRELLO_API_KEY = "test-key"
    TS._creds = lambda uid: {"key": "k", "token": f"token-of-{uid}"}
    TS.requests.request = tr.request
    tr.users = []
    real_call = TS._call.__wrapped__ if hasattr(TS._call, "__wrapped__") else TS._call

    def call(method, url, operation, uid="", **kw):   # who reached Trello
        tr.users.append(uid)
        return real_call(method, url, operation, uid, **kw)
    TS._call = call

    # no threads, no outside world
    sync.sync_async = lambda *a, **k: sync.sync_now(*a, **k)
    sync.invite_missing_async = lambda pid: sync.invite_missing(pid)
    sync.__dict__.setdefault("time", None)
    tool_trello.time = SimpleNamespace(sleep=lambda *_: None)
    tasks._notify_assignment_async = lambda **k: None
    tasks.threading = SimpleNamespace(Thread=lambda **k: SimpleNamespace(start=lambda: None))
    checkin._owner_email = lambda uid: E_OWNER
    return SimpleNamespace(P=P, T=T, tr=tr, tasks=tasks, sync=sync, PC=PC, projects=projects,
                           tool=tool_trello)


def new_req(**k):
    base = dict(text="Tarea", assigned_to="", status="pending", description="", start_date=None,
                due_date=None, parent_task_id=None)
    return SimpleNamespace(**{**base, **k})


def upd(**k):
    base = dict(text=None, status=None, assigned_to=None, description=None, blocked_reason=None,
                start_date=None, due_date=None, parent_task_id=None, comment=None)
    return SimpleNamespace(**{**base, **k})


# ── 1. member permissions ───────────────────────────────────────────────────

def test_member_permissions():
    print("\n1. What a member may do with tasks")
    W = world(linked=False)
    S = W.tasks
    mine = S.create_task(OWNER, E_OWNER, "p1", new_req(text="Landing", assigned_to="Ana Ruiz"))["taskId"]
    theirs = S.create_task(OWNER, E_OWNER, "p1", new_req(text="API", assigned_to="Beto Paz"))["taskId"]

    check("member cannot create a task", status_of(S.create_task, MEMBER, E_ANA, "p1", new_req()) == 403)
    check("member cannot delete a task", status_of(S.delete_task, MEMBER, E_ANA, mine, False) == 403)
    check("member cannot touch someone else's task",
          status_of(S.update_task, MEMBER, E_ANA, theirs, upd(status="done", comment="listo")) == 403)
    for field, val in (("text", "otro"), ("due_date", "2026-12-01"), ("assigned_to", "Beto Paz"),
                       ("description", "x"), ("start_date", "2026-11-01"), ("parent_task_id", "")):
        check(f"member cannot change {field} on their own task",
              status_of(S.update_task, MEMBER, E_ANA, mine, upd(**{field: val})) == 403)
    check("member status change WITHOUT a comment is refused (400)",
          status_of(S.update_task, MEMBER, E_ANA, mine, upd(status="in_progress")) == 400)
    check("... and a 2-letter comment is not a comment",
          status_of(S.update_task, MEMBER, E_ANA, mine, upd(status="in_progress", comment="ok")) == 400)
    check("nothing was written by any refused call", W.T.items[mine]["status"] == "pending"
          and W.T.items[mine]["text"] == "Landing" and "comments" not in W.T.items[mine])

    check("member status change WITH a comment is accepted",
          status_of(S.update_task, MEMBER, E_ANA, mine,
                    upd(status="in_progress", comment="Arranqué hoy con el hero")) == 200)
    t = W.T.items[mine]
    c = (t.get("comments") or [{}])[-1]
    check("status saved", t["status"] == "in_progress")
    check("comment saved with author and the change it explains",
          c.get("by") == E_ANA and c.get("fromStatus") == "pending" and c.get("toStatus") == "in_progress"
          and "hero" in c.get("text", ""), str(c))
    check("blocking: the blocked reason counts as the comment",
          status_of(S.update_task, MEMBER, E_ANA, mine,
                    upd(status="blocked", blocked_reason="Falta el dominio")) == 200
          and W.T.items[mine]["comments"][-1]["text"] == "Falta el dominio")
    check("member can add a comment without changing anything else",
          status_of(S.update_task, MEMBER, E_ANA, mine, upd(comment="Sigo esperando el dominio")) == 200
          and len(W.T.items[mine]["comments"]) == 3 and W.T.items[mine]["status"] == "blocked")
    check("someone outside the project gets 403",
          status_of(S.update_task, "uid-x", "x@x.co", mine, upd(status="done", comment="hola")) == 403)

    check("owner can edit anything, with no comment",
          status_of(S.update_task, OWNER, E_OWNER, theirs,
                    upd(text="API v2", due_date="2026-12-01", status="done")) == 200
          and W.T.items[theirs]["text"] == "API v2" and W.T.items[theirs]["status"] == "done")
    check("owner can delete", status_of(S.delete_task, OWNER, E_OWNER, theirs, False) == 200
          and theirs not in W.T.items)


# ── 2. decisions ────────────────────────────────────────────────────────────

def test_decisions_are_the_owners():
    print("\n2. Proposals are decided by the owner")
    W = world(linked=False)
    PC = W.PC
    check("member cannot approve (403)",
          status_of(PC.decide, MEMBER, E_ANA, "p1", "nope", "approve_all") == 403)
    check("member cannot reject (403)",
          status_of(PC.decide, MEMBER, E_ANA, "p1", "nope", "reject_all") == 403)
    check("member cannot revert (403)", status_of(PC.revert, MEMBER, E_ANA, "p1", "nope") == 403)
    # 404 = got past the permission gate and looked for the proposal
    check("owner passes the gate on decide", status_of(PC.decide, OWNER, E_OWNER, "p1", "nope", "approve_all") == 404)
    check("owner passes the gate on revert", status_of(PC.revert, OWNER, E_OWNER, "p1", "nope") == 404)
    check("the gate consolidation uses refuses a member", status_of(PC._owner_access, MEMBER, E_ANA, "p1") == 403)
    check("... and lets the owner through", status_of(PC._owner_access, OWNER, E_OWNER, "p1") == 200)
    check("members can still READ the planning chat", status_of(PC._access, MEMBER, E_ANA, "p1") == 200)


# ── 3. automatic sync ───────────────────────────────────────────────────────

def test_changes_reach_the_board():
    print("\n3. A change in OneBox reaches the board, with the OWNER's connection")
    W = world(linked=True)
    S, tr = W.tasks, W.tr
    tid = S.create_task(OWNER, E_OWNER, "p1", new_req(text="Landing", assigned_to="Ana Ruiz",
                                                      due_date="2026-11-20"))["taskId"]
    card = W.T.items[tid].get("trelloCardId")
    check("new task -> a card in the project's list",
          bool(card) and len(tr.did("POST", r"/1/cards", idList=L_HOME, name="Landing")) == 1)
    check("the task remembers its card and the sync hash",
          bool(W.T.items[tid].get("trelloSyncHash")) and W.T.items[tid].get("trelloCardListId") == L_HOME)

    tr.writes.clear(); tr.users.clear()
    S.update_task(MEMBER, E_ANA, tid, upd(status="done", comment="Publicado en producción"))
    check("member marks it done -> card moves to 'Hecho'",
          len(tr.did("PUT", rf"/1/cards/{card}", idList=L_DONE)) == 1, str(tr.writes))
    cm = tr.did("POST", rf"/1/cards/{card}/actions/comments")
    check("... and the comment is posted on the card",
          len(cm) == 1 and "Publicado en producción" in cm[0]["params"]["text"]
          and E_ANA in cm[0]["params"]["text"], str(cm))
    check("every Trello call used the OWNER's connection, not the member's",
          bool(tr.users) and set(tr.users) == {OWNER}, str(set(tr.users)))

    tr.writes.clear()
    S.update_task(OWNER, E_OWNER, tid, upd(status="pending"))
    check("reopened -> card goes back to the project's list",
          len(tr.did("PUT", rf"/1/cards/{card}", idList=L_HOME)) == 1, str(tr.writes))

    tr.writes.clear()
    S.update_task(OWNER, E_OWNER, tid, upd(text="Landing v2"))
    check("renamed in OneBox -> card renamed", len(tr.did("PUT", rf"/1/cards/{card}", name="Landing v2")) == 1)

    # somebody edits the card in Trello; OneBox must not overwrite it
    tr.cards[card]["name"] = "Landing (editada en Trello)"
    tr.writes.clear()
    S.update_task(OWNER, E_OWNER, tid, upd(text="Landing v3"))
    check("card edited in Trello is NOT overwritten", not tr.did("PUT", rf"/1/cards/{card}"), str(tr.writes))
    tr.cards[card]["name"] = "Landing v2"      # put it back as last synced

    tr.writes.clear()
    S.delete_task(OWNER, E_OWNER, tid, False)
    check("task deleted -> card archived (not deleted)",
          len(tr.did("PUT", rf"/1/cards/{card}", closed="true")) == 1 and not tr.did("DELETE", r".*"))

    W2 = world(linked=False)
    t2 = W2.tasks.create_task(OWNER, E_OWNER, "p1", new_req(text="Sin tablero"))["taskId"]
    W2.tasks.update_task(OWNER, E_OWNER, t2, upd(status="done"))
    W2.tasks.delete_task(OWNER, E_OWNER, t2, False)
    check("project without a board -> Trello is never called", not W2.tr.writes and not W2.tr.users)

    W3 = world(linked=True)
    W3.tr.request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Trello is down"))
    import api.services.trello as TS
    TS.requests.request = W3.tr.request
    ok = status_of(W3.tasks.create_task, OWNER, E_OWNER, "p1", new_req(text="Con Trello caído")) == 200
    check("Trello down -> the task is still saved in OneBox", ok and len(W3.T.items) == 1)


# ── 4. moved cards on re-export ─────────────────────────────────────────────

def test_moved_card_is_not_deleted():
    print("\n4. Re-export: a card in another column is found, not treated as deleted")
    W = world(linked=True)
    S, tr, tool = W.tasks, W.tr, W.tool
    tool._current_uid, tool._current_email = (lambda: OWNER), (lambda: E_OWNER)
    tool._has_project_access = lambda pid: True
    a = S.create_task(OWNER, E_OWNER, "p1", new_req(text="Bloqueada", assigned_to="Ana Ruiz"))["taskId"]
    b = S.create_task(OWNER, E_OWNER, "p1", new_req(text="Normal"))["taskId"]
    S.update_task(OWNER, E_OWNER, a, upd(status="blocked", blocked_reason="Espera a legal"))
    card_a = W.T.items[a]["trelloCardId"]
    check("setup: the blocked task's card sits in 'Bloqueado'", tr.cards[card_a]["idList"] == L_BLOCK)

    tr.writes.clear()
    out = tool.push_tasks_to_trello("p1")
    check("export succeeds", not out.get("error") and out.get("outcome") in ("complete", "noop"), str(out)[:300])
    check("no card is created again (no duplicates)", not tr.did("POST", r"/1/cards"), str(tr.writes))
    check("the moved card keeps its link", W.T.items[a].get("trelloCardId") == card_a)
    check("nothing reported as failed / deleted", out.get("failed_count") == 0, str(out.get("failed")))
    check("both tasks reported as already up to date", out.get("skipped_count") == 2, str(out.get("skipped")))

    tool._current_uid, tool._current_email = (lambda: MEMBER), (lambda: E_ANA)
    tr.writes.clear()
    out = tool.push_tasks_to_trello("p1")
    check("a member cannot run the export", out.get("owner_only") is True and not tr.writes, str(out)[:200])
    out = tool.link_project_to_trello("p1", list_name="Hecho")
    check("a member cannot re-link the project", out.get("owner_only") is True)
    out = tool.create_trello_board("Otro", project_id="p1")
    check("a member cannot create a board for the project",
          out.get("owner_only") is True and not tr.did("POST", r"/1/boards/?"))
    _ = b


# ── 5. invitations ──────────────────────────────────────────────────────────

def test_invitations():
    print("\n5. Board invitations: log, retry, and an email that arrives later")
    W = world(linked=True)
    tr, sync = W.tr, W.sync
    tr.reject = {E_BETO}
    r = sync.invite_missing("p1")
    log = {e.get("email") or e.get("name"): e for e in W.P.items["p1"].get("trelloInvites") or []}
    check("member with email is invited", r["invited"] == [E_ANA], str(r))
    check("the owner is not invited to their own board", E_OWNER not in log and
          not tr.did("PUT", rf"/1/boards/{BOARD}/members", email=E_OWNER))
    check("a refused invitation is recorded as failed, with Trello's reason",
          [f["email"] for f in r["failed"]] == [E_BETO] and log[E_BETO]["status"] == "failed"
          and log[E_BETO].get("error"), str(log.get(E_BETO)))
    check("someone without email is logged, not dropped",
          r["without_email"] == ["Carla"] and log["Carla"]["status"] == "no_email")
    check("invitations go out with the owner's connection", set(tr.users) == {OWNER})

    tr.reject, tr.writes = set(), []
    r = sync.invite_missing("p1")
    check("second run: the failed one is retried, the invited one is NOT invited again",
          r["invited"] == [E_BETO] and r["already"] == 1
          and not tr.did("PUT", rf"/1/boards/{BOARD}/members", email=E_ANA), str(r))

    tr.writes = []
    parts = [dict(p) for p in W.P.items["p1"]["participants"]]
    next(p for p in parts if p["name"] == "Carla")["email"] = "carla@x.co"
    W.projects.update_participants(OWNER, "p1", parts)
    log = {e.get("email"): e for e in W.P.items["p1"]["trelloInvites"]}
    check("Carla gets an email later -> she is invited automatically",
          len(tr.did("PUT", rf"/1/boards/{BOARD}/members", email="carla@x.co")) == 1
          and log.get("carla@x.co", {}).get("status") == "invited", str(tr.writes))
    check("... and nobody else is invited again", len(tr.writes) == 1)

    W2 = world(linked=False)
    r = W2.sync.invite_missing("p1")
    check("project without a board -> nothing is sent", r["status"] == "not_linked" and not W2.tr.writes)


if __name__ == "__main__":
    for t in (test_member_permissions, test_decisions_are_the_owners, test_changes_reach_the_board,
              test_moved_card_is_not_deleted, test_invitations):
        try:
            t()
        except Exception as e:      # a crash is a failure, with its place
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__} crashed: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS)))
    sys.exit(1 if FAILS else 0)
