"""Member updates -> consolidation -> approval reflected in Trello. Fake
tables, fake LLM, fake Trello. Run: python tests/test_member_updates.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.planning.nodes as nodes  # noqa: E402
import agent.tools as tools  # noqa: E402
import agent.tools.trello as tool_trello  # noqa: E402
import api.deps as deps  # noqa: E402
import api.services.access as access  # noqa: E402
import api.services.planning_live as PL  # noqa: E402
import api.services.trello as TS  # noqa: E402
from agent.planning.schemas import UpdateItem, UpdatePrep, UpdateTurn  # noqa: E402
from api.services import update_reminders as UR  # noqa: E402
from api.services import checkin as C  # noqa: E402
from api.services import member_updates as MU  # noqa: E402
from api.services import planning_chat as PC  # noqa: E402
from api.services import trello_reflect as TR  # noqa: E402
from api.services import updates_consolidation as UC  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok = ok and bool(cond)


class Table:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def _k(self, d):
        return d.get("itemId") or d.get("taskId") or d.get("projectId")

    def put_item(self, Item):
        self.items[self._k(Item)] = dict(Item)

    def get_item(self, Key):
        k = self._k(Key)
        return {"Item": dict(self.items[k])} if k in self.items else {}

    def delete_item(self, Key):
        self.items.pop(self._k(Key), None)

    def query(self, **_kw):
        return {"Items": [dict(v) for v in self.items.values()]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, ExpressionAttributeNames=None, **_kw):
        names = ExpressionAttributeNames or {}
        it = self.items.setdefault(self._k(Key), dict(Key))
        expr = UpdateExpression.strip()
        if expr.startswith("REMOVE"):
            for f in expr[6:].split(","):
                it.pop(f.strip(), None)
            return
        for part in expr[4:].split(","):
            lhs, rhs = [x.strip() for x in part.split("=")]
            it[names.get(lhs, lhs)] = ExpressionAttributeValues[rhs]


TODAY = "2026-06-05"
MU.TODAY = UC.TODAY = TODAY
import agent.planning.runner as RN  # noqa: E402
RN.default_llm = lambda *a, **k: object()
PROJ = {"projectId": "p1", "userId": "owner", "name": "OneBox", "planVersion": 3, "status": "active",
        "participants": [{"name": "Santi", "email": "santi@x.com"}, {"name": "Jeniffer", "email": "jen@x.com"},
                         {"name": "Jesús Alejandro Vega", "email": "ale@x.com"}, {"name": "Eloi"}]}
TASKS = {
    "ob-microsoft": {"taskId": "ob-microsoft", "text": "Integración Outlook", "status": "in_progress",
                     "assignedTo": "Jeniffer", "dueDate": "2026-06-06"},
    "ob-graficos": {"taskId": "ob-graficos", "text": "Gráficos front", "status": "in_progress", "assignedTo": "Jeniffer"},
    "ob-responsive": {"taskId": "ob-responsive", "text": "Responsive", "status": "pending", "assignedTo": "Jeniffer"},
    "ob-asana": {"taskId": "ob-asana", "text": "Asana parte 2", "status": "in_progress", "assignedTo": "Jeniffer"},
    "ob-seguridad": {"taskId": "ob-seguridad", "text": "Seguridad del chat", "status": "in_progress",
                     "assignedTo": "Jesús Alejandro Vega"},
    "ob-fechas": {"taskId": "ob-fechas", "text": "Fechas IA", "status": "pending", "assignedTo": "Jesús Alejandro Vega"},
    "ob-trello": {"taskId": "ob-trello", "text": "Seguimiento Trello", "status": "in_progress", "assignedTo": "Eloi"},
}
for t in TASKS.values():
    t["projectId"] = "p1"
projects, plan, tasks_t, notified = Table({"p1": PROJ}), Table(), Table(TASKS), []
tools.projects_table = projects
PC._table = plan
C._project_tasks = lambda pid: [dict(t) for t in tasks_t.items.values()]
C._owner_email = lambda uid: "santi@x.com"
access.accessible_project_ids = lambda uid, email: {"p1"}
access.has_project_access = lambda uid, email, pid: (True, False, PROJ)
PC._access = lambda uid, email, pid: PROJ
deps.scan_all_pages = lambda table, **kw: [PROJ]
PL.snapshot = lambda pid, *a, **k: {"project": dict(PROJ), "tasks": [dict(t) for t in tasks_t.items.values()],
                                     "insights": [], "attachments": []}
PL._notify = lambda owner, proj, title, msg, kind: notified.append((owner, title, kind, msg))
C._notify_owner_outside = lambda *a: notified.append(("outside", a[1], "outside", ""))
before_tasks = json.dumps(tasks_t.items, sort_keys=True)

# ── 1. the member chat: items recorded, plan untouched ─────────────────────
turns = []


PREPS = []


def fake_call(llm, schema, system, is_empty=None, **kw):
    if schema is UpdatePrep:
        PREPS.append(system)
        return UpdatePrep(subject="OneBox: ¿cómo vas?", greeting="Hola Jeniffer",
                          questions=["Lo de Outlook era para el 6, ¿cómo va?", "¿Avanzaste ob-responsive?",
                                     "¿Y los gráficos?"]), ""
    turns.append(system)
    return SCRIPTED.pop(0), ""


nodes.call_structured = fake_call
SCRIPTED = [
    UpdateTurn(reply="¿Qué le falta a Outlook y para cuándo?", finished=False, items=[
        UpdateItem(type="progress", task_id="ob-micro", text="Outlook casi listo", certainty="estimated", quote="casi listo"),
        UpdateItem(type="done", task_id="ob-graficos", text="Gráficos terminados", certainty="sure", quote="los graficos ya")]),
    UpdateTurn(reply="Resumen: Outlook casi (falta Azure, Santi), gráficos hechos, responsive el 12, Asana después del MVP.",
               finished=True, items=[
        UpdateItem(type="progress", task_id="ob-microsoft", text="Outlook casi listo", certainty="estimated", quote="casi listo"),
        UpdateItem(type="blocker", task_id="ob-microsoft", text="Falta la cuenta de Azure", owner="Santi", urgent=True,
                   certainty="sure", quote="falta la cuenta de azure"),
        UpdateItem(type="done", task_id="ob-graficos", text="Gráficos terminados", certainty="sure", quote="los graficos ya"),
        UpdateItem(type="date_change", task_id="ob-responsive", text="Responsive la otra semana", due_date="2026-06-12",
                   certainty="estimated", quote="la otra semana"),
        UpdateItem(type="idea", task_id="ob-asana", text="Dejar Asana para después del MVP", decision_for_owner=True,
                   quote="lo dejaria para despues del mvp"),
        UpdateItem(type="question", task_id="ob-inventado", text="¿Quién revisa el diseño?")])]
# ── 0. 'hora de actualizar': prepared questions, email, chat opening ──────
mails = []
UR._send = lambda proj, person, prep: mails.append((person["email"], prep)) or {"status": "sent"}
from datetime import datetime as _dt  # noqa: E402
r_first = UR.send_due(_dt(2026, 6, 5, UR.REMINDER_HOUR_UTC), llm=object())
check("first sight: nobody is emailed the day the cron starts (the modal asks them at login)",
      not r_first["sent"] and not mails, r_first)
for k in ("jen@x.com", "ale@x.com", "eloi"):
    C._save_state("p1", k, firstSeenAt="2026-05-20T00:00:00")
r0 = UR.send_due(_dt(2026, 6, 5, UR.REMINDER_HOUR_UTC), llm=object())
check("due members get the email with prepared questions (owner and no-email people do not)",
      [m[0] for m in mails] == ["jen@x.com", "ale@x.com"], mails)
check("the AI saw the member's tasks", PREPS and "ob-graficos" in PREPS[0])
check("a question that shows a task id is dropped", all("ob-" not in q for _e, p in mails for q in p["questions"]),
      mails[0][1]["questions"])
check("email text has the questions and the link", "Outlook" in UR.email_text(PROJ, mails[0][1]) and UR.APP_URL in
      UR.email_text(PROJ, mails[0][1]))
r0b = UR.send_due(_dt(2026, 6, 5, UR.REMINDER_HOUR_UTC + 2), llm=object())
early = UR.send_due(_dt(2026, 6, 6, UR.REMINDER_HOUR_UTC - 1), llm=object())
check("once per due period, and not before the reminder hour", not r0b["sent"] and not early["sent"], (r0b, early))
# testing mode (scripts/sim/update_flow.py eventbridge): no hour gate, owner included
PROJ["participants"].append({"name": "Santi", "email": "santi@x.com"})
tasks_t.items["ob-own"] = {"projectId": "p1", "taskId": "ob-own", "text": "Revisar contrato", "status": "pending",
                           "assignedTo": "Santi"}
C._save_state("p1", "santi@x.com", firstSeenAt="2026-05-20T00:00:00")
only = UR.send_due(_dt(2026, 6, 6, 3), llm=object(), force=True, only="nobody@x.com")
check("only=<email>: nobody else is emailed", not only["sent"], only)
forced = UR.send_due(_dt(2026, 6, 6, 3), llm=object(), force=True)
check("force: sends before the hour and includes the owner", [x["person"] for x in forced["sent"]] == ["Santi"],
      forced)
tasks_t.items.pop("ob-own")
PROJ["participants"].pop()

# the simulation script: status / make-due on the same fake world
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("sim", Path(__file__).resolve().parent.parent / "scripts/sim/update_flow.py")
SIM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SIM)
import types  # noqa: E402
SIM._owner = lambda proj: "santi@x.com"
SIM.cmd_make_due(types.SimpleNamespace(email="jen@x.com", project=""))
st = C._state("p1", "jen@x.com")
check("sim make-due: clock 4 days back, reminder/prep cleared",
      st.get("lastReportBy") == "sim:make-due" and not st.get("reminderSentAt") and not st.get("prep"), st)
SIM.cmd_status(types.SimpleNamespace(email="jen@x.com", project=""))
again = UR.send_due(_dt.utcnow(), llm=object(), force=True)      # make-due uses the real clock
check("after make-due the email goes out again", [x["person"] for x in again["sent"]] == ["Jeniffer"], again)

op = MU.open_session("u-jen", "jen@x.com", "p1")
check("the chat opens with the same prepared questions", op["messages"] and "Outlook" in op["messages"][0]["content"]
      and op["sessionId"] is None, op)

r1 = MU.converse("u-jen", "jen@x.com", "p1", ["hola! pues bien", "lo de microsoft casi listo", "los graficos ya"], llm=object())
check("first turn asks, not finished", not r1["finished"] and "?" in r1["reply"], r1)
check("a shortened task id is completed to the real one", r1["items"][0]["task_id"] == "ob-microsoft", r1["items"][0])
check("the prompt sees the member's own tasks and the owner", "ob-graficos" in turns[0] and "Santi" in turns[0])
check("no urgent alert before the update is closed", not notified)
r2 = MU.converse("u-jen", "jen@x.com", "p1", ["falta la cuenta de azure, eso lo tiene q crear santi",
                                              "responsive la otra semana",
                                              "y lo de asana yo lo dejaria para despues del mvp"], llm=object())
check("same session continues", r2["sessionId"] == r1["sessionId"], (r1["sessionId"], r2["sessionId"]))
check("the conversation starts with the prepared questions", "Outlook era para el 6" in turns[0])
check("the second turn sees the conversation so far", "los graficos ya" in turns[1] and "¿Qué le falta" in turns[1])
check("an invented task id is dropped", next(i for i in r2["items"] if i["type"] == "question")["task_id"] == "")
check("closed update: urgent blocker goes to the owner at once (app + outside)",
      any(n[2] == "update_urgent" and "Azure" in n[3] for n in notified) and any(n[0] == "outside" for n in notified),
      notified)
check("closed update resets the member's check-in clock", C._state("p1", "jen@x.com").get("lastReportAt"))
# the closing word must not flip what was said before (real failure: Ale's
# "listo" turned "seguridad va, me falta lo de los telefonos" into done)
G = MU.guard_items
prev = [{"type": "progress", "task_id": "s", "text": "Seguridad avanza, faltan teléfonos", "quote": "me falta lo de los telefonos"}]
flip = [{"type": "done", "task_id": "s", "text": "Seguridad lista", "quote": "listo"}]
check("guard: a closing-only turn changes nothing", G(prev, flip, ["listo"], ["seguridad va, me falta lo de los telefonos"]) == prev)
check("guard: a status flip without support in this turn is refused",
      G(prev, flip + [{"type": "not_started", "task_id": "f", "text": "Fechas sin tocar", "quote": "ni lo he tocado"}],
        ["lo de las fechas ni lo he tocado"], ["seguridad va, me falta lo de los telefonos"]) ==
      prev + [] or [i["type"] for i in G(prev, flip, ["lo de las fechas ni lo he tocado"], ["x"])] == ["progress"])
said = [{"type": "done", "task_id": "s", "text": "Seguridad terminada", "quote": "ya quedo lo de seguridad"}]
check("guard: a flip the member did say in this turn is accepted",
      [i["type"] for i in G(prev, said, ["ya quedo lo de seguridad"], ["seguridad va"])] == ["done"])
check("guard: an item the model silently dropped comes back",
      G(prev, [{"type": "not_started", "task_id": "f", "text": "Fechas sin tocar", "quote": "ni lo he tocado"}],
        ["lo de las fechas ni lo he tocado"], ["seguridad va, me falta lo de los telefonos"])[-1] == prev[0])
check("guard: an invented item (not in the member's words) is dropped",
      G([], [{"type": "done", "task_id": "x", "text": "Login terminado", "quote": "login listo"}], ["los graficos ya"], []) == [])
check("the plan did not change", json.dumps(tasks_t.items, sort_keys=True) == before_tasks)
check("no proposal yet", not [i for i in plan.items if i.startswith("prop#")])
check("closed session listed for consolidation", len(MU.sessions("p1")) == 1)

# Ale: hearsay about Jenny's task, a conditional plan, low-certainty 'done'
SCRIPTED = [UpdateTurn(reply="Listo, gracias.", finished=True, items=[
    UpdateItem(type="progress", task_id="ob-seguridad", text="Filtra por proyecto, faltan teléfonos", certainty="sure",
               quote="me falta lo de los telefonos"),
    UpdateItem(type="plan", task_id="ob-fechas", text="Fechas cuando termine seguridad", certainty="conditional",
               depends_on="ob-seguridad", due_date="2026-06-10", quote="depende de si termino seguridad"),
    UpdateItem(type="hearsay", task_id="ob-microsoft", about_person="Jeniffer", certainty="low",
               text="Cree que Jenny terminó Outlook", quote="creo q jenny ya termino lo de microsoft")])]
MU.converse("u-ale", "ale@x.com", "p1", ["lo de seguridad va, me falta lo de los telefonos",
                                         "lo de las fechas depende de si termino seguridad",
                                         "creo q jenny ya termino lo de microsoft"], llm=object())

# ── 2. consolidation: guard rules ──────────────────────────────────────────
tasks = {k: dict(v) for k, v in tasks_t.items.items()}
b = UC.build(PROJ, tasks, MU.sessions("p1"), None, TODAY)
ops = {(o["op"], o["task_id"]) for o in b["operations"]}
check("done by the assignee -> close", ("close", "ob-graficos") in ops, ops)
check("blocker -> block with who must act", any(o["op"] == "block" and o["task_id"] == "ob-microsoft" and "Santi" in
                                                  o["blocked_reason"] for o in b["operations"]), b["operations"])
check("hearsay never closes", ("close", "ob-microsoft") not in ops)
check("Jenny 'casi' vs Ale 'creo que terminó' is a contradiction for the owner",
      [c["task_id"] for c in b["contradictions"]] == ["ob-microsoft"] and "Jeniffer" in b["contradictions"][0]["text"]
      and "Jesús" in b["contradictions"][0]["text"], b["contradictions"])
check("estimated date -> update marked estimated", any(o["op"] == "update" and o["task_id"] == "ob-responsive" and
                                                       o.get("date_status") == "estimated" for o in b["operations"]))
check("conditional date does NOT move the date, becomes a dependency",
      not any(o["task_id"] == "ob-fechas" and o.get("due_date") for o in b["operations"])
      and any(o["op"] == "link" and o["task_id"] == "ob-fechas" and o["depends_on"] == ["ob-seguridad"] for o in b["operations"]))
check("an idea only the owner decides is a question, never an operation on the task",
      not any(o["task_id"] == "ob-asana" for o in b["operations"]) and any("Asana" in q["text"] for q in b["questions"]))
check("nothing unassigned in the fake project", b["unassigned"] == [] and b["strangers"] == {}, (b["unassigned"], b["strangers"]))
_t2 = {**tasks, "x1": {"taskId": "x1", "text": "Sin dueño", "status": "pending", "assignedTo": ""},
       "x2": {"taskId": "x2", "text": "De Rafa", "status": "pending", "assignedTo": "Rafael Sardinha"}}
_b2 = UC.build(PROJ, _t2, [], None, TODAY)
check("digest data: unassigned tasks and assignees outside the project",
      _b2["unassigned"] == ["Sin dueño"] and _b2["strangers"] == {"Rafael Sardinha": 1}, (_b2["unassigned"], _b2["strangers"]))
check("digest text says so", "sin responsable" in UC.digest_text(PROJ, _t2, _b2, TODAY) and
      "Rafael Sardinha (1)" in UC.digest_text(PROJ, _t2, _b2, TODAY))
check("who did not report is listed (owner excluded)", b["not_reported"] == ["Eloi"], b["not_reported"])

r = UC.consolidate("p1", actor="santi@x.com", send_external=False)
props = [v for k, v in plan.items.items() if k.startswith("prop#")]
check("consolidation saves ONE pending proposal from 'updates'", len(props) == 1 and props[0]["status"] == "pending"
      and props[0]["source"] == "updates", [(p.get("source"), p.get("status")) for p in props])
digest = r["digest"]
check("digest has urgent, contradiction, decision and who did not report",
      all(w in digest for w in ("URGENTE", "Azure", "No coinciden", "Asana", "No reportaron: Eloi")), digest)
check("digest posted in the planning chat with the proposal",
      any(v.get("kind") == "updates_digest" and v.get("proposalId") == r["proposalId"] for v in plan.items.values()))
check("owner notified with the digest", any(n[2] == "updates_digest" for n in notified))
check("sessions marked consolidated", MU.sessions("p1") == [])
check("still nothing applied", json.dumps(tasks_t.items, sort_keys=True) == before_tasks)
check("nothing new -> nothing to consolidate", UC.consolidate("p1", send_external=False)["status"] == "nothing")

# cron: once a day after the digest hour
from datetime import datetime  # noqa: E402
SCRIPTED = [UpdateTurn(reply="ok", finished=True, items=[UpdateItem(type="progress", task_id="ob-trello", text="al día")])]
MU.converse("u-eloi", "", "p1", ["todo al día"], name="Eloi", llm=object())
early = UC.consolidate_due(datetime(2026, 6, 6, UC.DIGEST_HOUR_UTC - 1), send_external=False)
late = UC.consolidate_due(datetime(2026, 6, 6, UC.DIGEST_HOUR_UTC), send_external=False)
again = UC.consolidate_due(datetime(2026, 6, 6, UC.DIGEST_HOUR_UTC + 1), send_external=False)
check("cron waits for the digest hour, runs once a day", not early["projects"] and late["projects"] and not again["projects"],
      (early, late, again))

# owner vs member, and the owner updating someone without an account
C._save_state("p1", "jen@x.com", lastReportAt="2026-01-01T00:00:00")     # her update is due again
pm = C.pending_for_user("u-jen", "jen@x.com")["projects"]
po = C.pending_for_user("owner", "jen@x.com")["projects"]
check("members cannot postpone the update, the owner can", pm and not pm[0]["isOwner"] and po and po[0]["isOwner"],
      (pm[:1], po[:1]))
PC._count_as_reports({"project": PROJ, "tasks": [dict(t) for t in tasks_t.items.values()]}, {"ob-trello"},
                     "2026-06-07T10:00:00", "santi@x.com")
check("an approved change to Eloi's task counts as his report (owner updated him)",
      C._state("p1", "eloi").get("lastReportBy") == "owner:santi@x.com", C._state("p1", "eloi"))

# ── 3. approval reflected in Trello ────────────────────────────────────────
calls = []
LISTS = [{"listId": "L-home", "name": "Sprint"}, {"listId": "L-block", "name": "Bloqueado"},
         {"listId": "L-done", "name": "Hecho ✅"}]
base_t = {"taskId": "t-edit", "text": "Editada en Trello", "status": "in_progress", "dueDate": "", "assignedTo": "Jeniffer"}
CARDS = {
    "L-home": [
        {"cardId": "c-graf", "name": "Gráficos front", "desc": "", "due": "", "listId": "L-home", "closed": False},
        {"cardId": "c-old", "name": "Vieja", "desc": "", "due": "", "listId": "L-home", "closed": False},
        {"cardId": "c-edit", "name": "Cambiado por alguien", "desc": "", "due": "", "listId": "L-home", "closed": False}],
    "L-block": [], "L-done": []}
TS.list_board_lists = lambda uid, board: LISTS
TS.list_cards = lambda uid, lid: CARDS[lid]
TS.create_card = lambda uid, lid, name, desc="", due="": calls.append(("create", lid, name)) or {"cardId": "c-new", "url": "u"}
TS.update_card = lambda uid, cid, name="", desc="", due="", list_id="", closed=False: \
    calls.append(("update", cid, list_id, closed)) or calls.append(("fields", cid, name, due)) or {"cardId": cid}
tool_trello.tasks_table = tasks_t
fp = lambda t: tool_trello._card_fingerprint(t["text"], tool_trello._card_desc(t), t.get("dueDate", ""))  # noqa: E731
graf_before = {**TASKS["ob-graficos"], "trelloCardId": "c-graf", "trelloCardListId": "L-home"}
graf_before["trelloSyncHash"] = tool_trello._card_fingerprint("Gráficos front", "", "")
CARDS["L-home"][0]["desc"] = tool_trello._card_desc(graf_before)
graf_before["trelloSyncHash"] = fp(graf_before)
old_t = {"taskId": "t-old", "text": "Vieja", "status": "pending", "trelloCardId": "c-old"}
edit_before = {**base_t, "trelloCardId": "c-edit", "trelloCardListId": "L-home"}
edit_before["trelloSyncHash"] = fp(edit_before)
after = {"ob-graficos": {**graf_before, "status": "done"},
         "t-new": {"taskId": "t-new", "text": "Daily de 15 min", "status": "pending", "projectId": "p1"},
         "t-edit": {**edit_before, "dueDate": "2026-06-20"}}
tasks_t.items.update({k: {**v, "projectId": "p1"} for k, v in after.items()})
linked = {**PROJ, "trelloBoardId": "B1", "trelloListId": "L-home"}
res = TR.reflect("owner", linked, {"ob-graficos": graf_before, "t-old": old_t, "t-edit": edit_before}, after,
                 ["ob-graficos", "t-old", "t-edit"], ["t-new"])
check("closed task -> card moved to the 'Hecho' column", ("update", "c-graf", "L-done", False) in calls, calls)
check("created task -> new card in the project's list", ("create", "L-home", "Daily de 15 min") in calls, calls)
check("new card linked back to the task", tasks_t.items["t-new"].get("trelloCardId") == "c-new")
check("deleted task -> card archived, not deleted", ("update", "c-old", "", True) in calls, calls)
check("a card edited in Trello is not overwritten", not any(c[1] == "c-edit" for c in calls)
      and any("Trello" in s for s in res["skipped"]), (calls, res))
check("summary for the chat", "movidas" in TR.summary(res) and "archivadas" in TR.summary(res), TR.summary(res))
CARDS["L-home"].append({"cardId": "c-hand", "name": "Responsive móvil / iPad", "desc": "a mano", "due": "",
                       "listId": "L-home", "closed": False})
hand = {"taskId": "t-hand", "text": "Responsive", "status": "pending", "trelloCardId": "c-hand", "projectId": "p1"}
calls.clear()
TR.reflect("owner", linked, {"t-hand": hand}, {"t-hand": {**hand, "dueDate": "2026-06-12"}}, ["t-hand"], [])
check("card linked by hand (never synced): only the date is written, its title is kept",
      ("fields", "c-hand", "", "2026-06-12") in calls, calls)
check("not linked -> nothing written", TR.reflect("owner", PROJ, {}, {}, ["x"], [])["status"] == "not_linked")

print("\nOK" if ok else "\nFAILED")
sys.exit(0 if ok else 1)
