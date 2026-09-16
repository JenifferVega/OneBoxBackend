"""The four boxes of the three-way comparison, on the REAL functions."""
import ast, sys, types

src = open("/home/claude/v10/trello.py").read()
ns = {"time": types.SimpleNamespace(sleep=lambda *_: None)}
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in (
            "_card_desc", "_card_fingerprint", "_diff_task_and_card"):
        exec(compile(ast.Module([n], []), "x", "exec"), ns)

desc, fp, diff = ns["_card_desc"], ns["_card_fingerprint"], ns["_diff_task_and_card"]


def task(text="Migrar el backend", due="2026-09-01", status="pending",
         assigned="Ana", tid="t1"):
    return {"text": text, "dueDate": due, "status": status,
            "assignedTo": assigned, "taskId": tid}


def card_from(t, **over):
    c = {"name": t["text"], "desc": desc(t), "due": t.get("dueDate", ""),
         "listId": "L1", "lastActivity": "2026-09-14T10:00:00Z"}
    c.update(over)
    return c


def synced(t, list_id="L1"):
    """A task whose base matches what is on the card right now."""
    t = dict(t)
    t["trelloSyncHash"] = fp(t["text"], desc(t), t.get("dueDate", ""))
    t["trelloCardId"] = "c1"
    t["trelloCardListId"] = list_id
    return t


ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got}" + ("" if good else f" (expected {want})"))


print("\nThe four boxes")
base = synced(task())
check("neither side moved", diff(base, card_from(task()))["verdict"], "unchanged")

moved_local = dict(base); moved_local["text"] = "Migrar el backend a FastAPI"
check("only OneBox moved", diff(moved_local, card_from(task()))["verdict"], "local")

check("only Trello moved",
      diff(base, card_from(task(), name="Migrar backend (URGENTE)"))["verdict"], "remote")

check("both moved -> conflict",
      diff(moved_local, card_from(task(), name="Migrar backend (URGENTE)"))["verdict"],
      "conflict")

print("\nA card dragged to another column")
check("column move alone is a remote change",
      diff(base, card_from(task(), listId="L2"))["verdict"], "remote")
check("column move + local edit is a conflict",
      diff(moved_local, card_from(task(), listId="L2"))["verdict"], "conflict")
check("the moved column is named",
      "column" in diff(base, card_from(task(), listId="L2"))["fields"], True)

print("\nWhich fields moved")
d = diff(base, card_from(task(), name="otro", due="2026-12-01"))
check("title and due date reported", sorted(d["fields"]), ["due date", "title"])

print("\nA task exported before the base field existed")
no_base = dict(base); no_base.pop("trelloSyncHash")
check("adopts the base instead of guessing",
      diff(no_base, card_from(task()))["verdict"], "adopt_base")

print("\nThe destructive case this exists to prevent")
d = diff(base, card_from(task(), name="Editado a mano en Trello"))
check("a hand-edited card is never 'local'", d["verdict"] != "local", True)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
