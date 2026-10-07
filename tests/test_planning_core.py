"""Planning graph, deterministic parts + full graph with a scripted LLM.

No network, no AWS, no real model. Run: python tests/test_planning_core.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.planning.builder import build_planning_graph  # noqa: E402
from agent.planning.runner import run_planning  # noqa: E402
from agent.planning.schedule import analyze_plan, apply_operations, apply_proposal, propagate  # noqa: E402
from agent.planning.schemas import (  # noqa: E402
    CoverOutput, FactsOutput, Intent, LinkOutput, ReconcileOutput, TextOutput)
from agent.planning.sources import match_person, redact, whatsapp_delta  # noqa: E402
from agent.planning.validate import validate  # noqa: E402
from tests.planning.evaluate import evaluate  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    ok = ok and bool(cond)


V1 = "[9/11/26, 9:55:17 AM] David: hola\n[9/12/26, 2:00:53 PM] Claudia: pendiente X\n"
V2 = V1 + "[9/17/26, 6:42:46 PM] Claudia: Tag Manager ya funciona\nsegunda linea\n[9/21/26, 5:34:50 PM] Claudia: Habemus cuenta de Meta!\n"

# ── sources ──
delta, meta = whatsapp_delta(V2, [V1])
check("delta keeps only messages after the previous export", "Tag Manager" in delta and "pendiente X" not in delta, delta)
check("delta keeps continuation lines", "segunda linea" in delta)
check("delta reports skipped messages", meta.get("skipped") == 2, meta)
txt, n = redact("Email: a@b.c\nPassword: AccountDefaultPass26@#")
check("password redacted", n == 1 and "AccountDefault" not in txt)
check("display name matches participant", (match_person("Claudia Ads Google", [{"name": "Claudia"}]) or {}).get("name") == "Claudia")

# ── schedule ──
T = [
    {"taskId": "a", "text": "Acceso", "status": "pending", "startDate": "2026-10-01", "dueDate": "2026-10-05", "createdBy": "IA", "dependsOn": []},
    {"taskId": "b", "text": "Campaña", "status": "pending", "startDate": "2026-10-03", "dueDate": "2026-10-04", "createdBy": "IA", "dependsOn": ["a"]},
    {"taskId": "h", "text": "Humana", "status": "blocked", "createdBy": "user-1", "dependsOn": []},
]
draft, _c, err = apply_operations(T, [{"op": "close", "task_id": "zz"}])
check("unknown task id is an error", err)
draft, _c, _e = apply_operations(T, [])
shifts = propagate(draft)
check("dependency pushes the dependent task", shifts and shifts[0]["to"][0] == "2026-10-06", shifts)
an = analyze_plan(T, {"deliveryDate": "20226-09-30"}, [], date(2026, 10, 10))
kinds = {i["kind"] for i in an["incongruences"]}
check("invalid delivery date detected", "invalid_delivery_date" in kinds)
check("overdue detected", "overdue" in kinds)
check("blocked without reason detected", "blocked_without_reason" in kinds)

# ── validate ──
v = validate(T, [{"op": "delete", "task_id": "h", "evidence": [{"quote": "q"}]},
                 {"op": "close", "task_id": "a"},
                 {"op": "link", "task_id": "a", "depends_on": ["b"]}], [], [])
check("deleting human work becomes a question", v["questions"] and not any(o["op"] == "delete" for o in v["operations"]))
check("close without evidence is an error", any("evidence" in e for e in v["errors"]))
check("cycle is an error", any("cycle" in e for e in v["errors"]))

# ── apply ──
new, events = apply_proposal({"project": {"planVersion": 2}, "tasks": T},
                             {"operations": [{"op": "close", "task_id": "a", "reason": "r", "evidence": []}]})
check("apply bumps planVersion", new["project"]["planVersion"] == 3)
check("apply records events", [e["op"] for e in events][:1] == ["close"])


# ── Android exports (d/m, " - " separator) and bare secrets ──
from agent.planning.sources import whatsapp_messages  # noqa: E402
AND = ("19/10/25, 23:43 - Ana: Fakepass123***\n4/6/26, 12:15 - Ana: listo el informe\n"
       "4/6/26, 12:16 - Ana: G-123456\n4/6/26, 12:17 - Ana: mi correo es ana@x.com\n"
       "4/6/26, 12:18 - Ana: segunda linea\nsigue aqui")
msgs = whatsapp_messages(AND)
check("android lines parsed, d/m order", len(msgs) == 5 and msgs[1][0].month == 6 and msgs[1][0].day == 4, msgs[:2])
check("continuation line kept with its message", msgs[-1][1].endswith("sigue aqui"))
red, n = redact(AND)
check("bare password and code redacted, email kept", n == 2 and "Fakepass" not in red and "ana@x.com" in red, red)
IOS = "[9/21/26, 5:34:50 PM] Claudia: hola\n[9/22/26, 9:00:00 AM] Claudia: chao\n[9/23/26, 9:00:00 AM] C: x"
check("iOS lines still parsed (m/d)", whatsapp_messages(IOS)[0][0].day == 21)
from agent.planning import nodes as _N  # noqa: E402
big = "\n".join(f"{(i % 28) + 1}/5/26, 10:{i % 60:02d} - Ana: " + "x" * 200 for i in range(300))
ch = _N._chunks(big)
check("android chat is chunked on message starts", len(ch) >= 5 and max(len(c) for c in ch) <= 15000, [len(c) for c in ch])
check("a text without any boundary is still cut", max(len(c) for c in _N._chunks("y" * 40 + ("z" * 100 + "\n") * 400)) <= 15100)


# ── full graph with a scripted LLM ──
class Scripted:
    def __init__(self, answers):
        self.answers = answers

    def with_structured_output(self, schema):
        outer = self

        class R:
            def invoke(self, _msgs):
                return outer.answers.get(schema.__name__, schema())
        return R()


EV = [{"source": "chat.txt", "locator": "9/17 18:42", "quote": "Tag Manager ya funciona"}]
LLM = Scripted({
    "FactsOutput": FactsOutput(facts=[{"kind": "done", "text": "Acceso conseguido", "evidence": EV}]),
    "ReconcileOutput": ReconcileOutput(
        operations=[{"op": "close", "task_id": "a", "reason": "hecho", "evidence": EV},
                    {"op": "create", "task_id": "new-1", "text": "Ignorada: los batches no crean", "evidence": EV}],
        summary="1 cierre"),
    "CoverOutput": CoverOutput(
        coverage=[{"fact": 1, "action": "create", "task_id": "x1"}],
        creates=[{"op": "create", "task_id": "x1", "text": "Subir videos a YouTube", "evidence": EV}],
        notes=[{"kind": "email", "text": "Claudia envió el email de accesos", "date": "2026-09-14",
                "task_id": "x1", "evidence": EV},
               {"kind": "meeting", "text": "Reunión sin evidencia"}],
        summary="1 nueva"),
    "LinkOutput": LinkOutput(links=[{"op": "link", "task_id": "new-1", "depends_on": ["b"], "reason": "x"}]),
    "TextOutput": TextOutput(text="Propuesta lista"),
    "Intent": Intent(kind="question", instruction="¿qué está atrasado?"),
})
EXPORT = {"project": {"projectId": "p1", "name": "Demo", "participants": [{"name": "Claudia"}]},
          "tasks": T, "attachments": [{"fileName": "chat.txt", "createdAt": "1", "fullText": V2}]}
graph = build_planning_graph(LLM)
check("graph compiles", graph is not None)
res = run_planning(EXPORT, "reanalyze", today="2026-10-05", llm=LLM)
p = res["proposal"] or {}
check("reanalyze produces a proposal", p.get("counts") == {"close": 1, "create": 1}, p.get("counts"))
check("batch creates are dropped, cover creates kept",
      [o["text"] for o in p.get("operations", []) if o["op"] == "create"] == ["Subir videos a YouTube"])
check("note without evidence dropped, note linked to the new task",
      len(p.get("notes") or []) == 1 and p["notes"][0]["task_id"].startswith("new-"), p.get("notes"))
check("coverage recorded per fact", p.get("coverage") and p["coverage"][0]["action"] == "create", p.get("coverage"))
check("link merged into its create", any(o["op"] == "create" and o.get("depends_on") == ["b"] for o in p.get("operations", [])))
nodes = [t["node"] for t in res["trace"]]
check("reanalyze path", nodes[:5] == ["load_project", "load_sources", "extract_facts", "consolidate", "reconcile"], nodes)
res = run_planning(EXPORT, "chat", today="2026-10-05", message="¿qué está atrasado?", llm=LLM)
check("chat question path answers", res["answer"] == "Propuesta lista" and res["proposal"] is None,
      [t["node"] for t in res["trace"]])
LLM.answers["Intent"] = Intent(kind="decide", decision="approve_all", instruction="aprueba todo")
res2 = run_planning(EXPORT, "chat", today="2026-10-05", message="aprueba todo", llm=LLM, open_proposal=p)
check("chat approve applies the proposal",
      res2["result_export"] and any(t["taskId"] == "a" and t["status"] == "done" for t in res2["result_export"]["tasks"]))
ins = (res2["result_export"] or {}).get("insights") or []
newt = [t["taskId"] for t in (res2["result_export"] or {}).get("tasks", []) if t.get("createdBy") == "planning"]
check("approval writes notes as insights of type 'note' linked to the created task",
      len(ins) == 1 and ins[0]["type"] == "note" and ins[0]["noteKind"] == "email" and ins[0]["taskId"] in newt, ins)

# ── big plans are judged in batches, creates in their own call ──
from agent.planning import nodes as N  # noqa: E402
calls = []


class Recorder:
    def with_structured_output(self, schema):
        class R:
            def invoke(self, msgs):
                sysmsg = msgs[0].content
                calls.append(sysmsg)
                if schema is ReconcileOutput:
                    ops = [{"op": "close", "task_id": "t0", "evidence": EV, "reason": "r"},
                           {"op": "create", "text": "Nueva", "evidence": EV}]
                    revs = [{"task_id": f"t{i}", "verdict": "close" if i == 0 else "keep"} for i in range(40)]
                    return ReconcileOutput(reviews=revs, operations=ops, summary="s")
                if schema is CoverOutput:
                    # first answer forgets F2 -> the node must ask again
                    cov = [{"fact": 1, "action": "create", "task_id": "new-1"}]
                    if "left these topics undecided" in sysmsg:
                        cov.append({"fact": 2, "action": "covered", "task_id": "t3"})
                    return CoverOutput(coverage=cov, creates=[{"op": "create", "task_id": "new-1", "text": "Nueva",
                                                               "evidence": EV}])
                return schema()
        return R()


BIG = [{"taskId": f"t{i}", "text": f"Tarea {i}", "status": "pending", "createdBy": "IA", "dependsOn": []} for i in range(40)]
FACTS2 = [{"kind": "request_to_client", "text": "Subir videos", "evidence": EV},
          {"kind": "done", "text": "Tarea 3 hecha", "evidence": EV}]
st = {"snapshot": {"project": {}, "participants": [], "tasks": BIG}, "facts": FACTS2, "trace": []}
out = N.reconcile(st, Recorder())
check("40 tasks -> 3 batch calls + cover call + 1 cover retry for the missing fact", len(calls) == 5, len(calls))
check("batches see the new tasks proposed by the cover call", sum("new-1 | NEW (proposed)" in c for c in calls) == 3, len(calls))
check("cover retry fills the gap", [c["fact"] for c in out["coverage"]] == [1, 2], out["coverage"])
check("batch calls only keep ops on their own tasks", [o["task_id"] for o in out["operations"] if o["op"] == "close"] == ["t0"],
      out["operations"])
check("only the cover call creates", sum(1 for o in out["operations"] if o["op"] == "create") == 1)
check("long material is chunked", len(N._chunks("[1/1/26, 1:00:00 PM] a: " + "x" * 100 + "\n" * 1 + ("[1/1/26, 1:00:01 PM] b: " + "y" * 900 + "\n") * 40)) >= 3)

# ── consolidate: few facts -> one topic per fact (no LLM); many -> LLM topics, orphans kept ──
from agent.planning.schemas import TopicsOutput  # noqa: E402
few = N.consolidate({"facts": [{"kind": "done", "text": "Número comprado", "date": "2026-03-05", "evidence": EV}],
                     "snapshot": {"project": {}, "participants": [], "tasks": []}, "trace": []}, None)
check("few facts: one topic each, state from kind, no LLM",
      few["topics"][0]["state"] == "done" and few["trace"][-1]["llm"] is False, few["topics"])
MANY = [{"kind": "new_work", "text": f"Hecho {i}", "date": f"2026-03-{i % 28 + 1:02d}", "evidence": EV} for i in range(25)]


class TopicLLM:
    def with_structured_output(self, schema):
        class R:
            def invoke(self, _m):
                return TopicsOutput(topics=[{"topic": "Twilio", "state": "done", "summary": "comprado y conectado",
                                             "facts": list(range(1, 21)), "last_date": "2026-04-07"},
                                            {"topic": "Raro", "state": "weird", "facts": [21]}])
        return R()


many = N.consolidate({"facts": MANY, "people": [], "snapshot": {"project": {}, "participants": [], "tasks": []},
                      "trace": []}, TopicLLM())
check("many facts: LLM topics + orphan facts kept as info topics", len(many["topics"]) == 6 and sum(t["state"] == "info" for t in many["topics"]) == 4, many["topics"])
check("unknown topic state normalised to open", any(t["topic"] == "Raro" and t["state"] == "open" for t in many["topics"]))
check("topic without evidence takes its last fact's", all(t["evidence"] for t in many["topics"]))
check("topics block numbers T1..", N._topics_block(many["topics"]).startswith("T1. ["))


# ── a whole answer returned as a JSON string inside one field is repaired ──
import json as _json  # noqa: E402
_inner = {"reviews": [{"task_id": "a", "verdict": "close"}], "operations": [{"op": "close", "task_id": "a"}]}
_rep = N._repair(ReconcileOutput, {"operations": _json.dumps(_inner)})
check("answer nested as a string is repaired", _rep is not None and _rep.operations[0].op == "close", _rep)


# ── stale topics and near-identical new tasks are handled by code ──
TOP = [{"topic": "Twilio", "state": "open", "last_date": "2026-03-05"},
       {"topic": "Seguridad", "state": "open", "last_date": "2026-05-31"},
       {"topic": "Microsoft", "state": "open", "last_date": "2026-03-01", "due_date": "2026-06-05"},
       {"topic": "Dominio", "state": "done", "last_date": "2026-03-10"},
       {"topic": "Sin fecha", "state": "open", "last_date": ""}]
check("stale = open, old, no deadline", N._stale_topics(TOP) == {1}, N._stale_topics(TOP))
kept, merged = N._merge_same_creates([{"task_id": "new-1", "text": "Integrar Slack con la plataforma"},
                                      {"task_id": "new-2", "text": "Integrar la plataforma con Slack"},
                                      {"task_id": "new-3", "text": "Presentar evoluciones"}])
check("near-identical creates merged", [k["task_id"] for k in kept] == ["new-1", "new-3"] and merged == {"new-2": "new-1"},
      merged)


# ── every task of a batch needs a review that matches the operations ──
from agent.planning.schemas import ReconcileOutput as RO  # noqa: E402
g = N._review_gaps(RO(reviews=[{"task_id": "a", "verdict": "close"}], operations=[]),
                   [{"taskId": "a"}, {"taskId": "b"}])
check("review gaps: missing review and verdict without operation", len(g) == 2, g)
ops2 = N._close_instead_of_delete([{"taskId": "x", "status": "pending"}, {"taskId": "y", "status": "done"}],
                                  [{"op": "delete", "task_id": "x", "duplicate_of": "y", "reason": "dup"}])
check("a duplicate of done work is closed, not deleted", ops2[0]["op"] == "close" and not ops2[0]["duplicate_of"], ops2)

# ── a replaced task may be a duplicate of a task created in the same proposal ──
v = validate(T, [{"op": "create", "task_id": "new-1", "text": "Mapa AS-IS", "evidence": EV},
                 {"op": "delete", "task_id": "a", "duplicate_of": "new-1", "evidence": EV}], [], [])
check("duplicate_of may reference a create of the same proposal", not v["errors"], v["errors"])
from tests.planning.evaluate import _matches  # noqa: E402
check("plan words match at word start", _matches("Sesión de formación", [["formacion"]])
      and not _matches("información de entrada", [["formacion"]]))

# ── evaluator ──
r = evaluate(p, EXPORT, {"existing": {"a": {"accept": ["close"]}},
                         "plan_items": [{"id": "yt", "any_of": [["youtube"]]}]})
check("evaluator scores a matching proposal as passed", r["passed"], r)

print("\nOK" if ok else "\nFAILED")
sys.exit(0 if ok else 1)
