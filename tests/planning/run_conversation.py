"""Run a scripted planning-chat conversation (tests/planning/conversations/<name>)
against the REAL LLM, with every AWS table faked in memory, and grade it
turn by turn.

    python tests/planning/run_conversation.py bion_chat [--upto 5]
    python tests/planning/run_conversation.py --list

The conversation goes through api.services.planning_chat exactly as the app
does (start_message -> graph -> proposal; "dale" -> decide), so what is graded
is the product, not a test double. Trello reads go through
api.services.trello with `requests` answered from fixtures/trello_api.json
(raw Trello REST responses); documents go through the real extractor.

Output: conversations/<name>/runs/<ts>/ and runs/latest/ with report.md,
result.json and transcript.json. Exit code is always 0 (a failing turn is a
finding, not a crash).
"""
import argparse
import copy
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CONV = Path(__file__).resolve().parent / "conversations"

from agent.planning.sources import norm  # noqa: E402
from tests.planning.evaluate import _matches  # noqa: E402

LEADER_UID = "leader-uid"


# ── a fake AWS ──────────────────────────────────────────────────────────────

class Table:
    """Enough of a DynamoDB Table for the code paths of the planning chat."""

    def __init__(self, key, items=None):
        self.key = key
        self.items = {}
        for it in items or []:
            self.put_item(Item=it)

    def _k(self, d):
        return d.get(self.key) or d.get("itemId") or d.get("taskId") or d.get("insightId") or d.get("projectId")

    def put_item(self, Item):
        self.items[self._k(Item)] = copy.deepcopy(Item)

    def get_item(self, Key):
        k = self._k(Key)
        return {"Item": copy.deepcopy(self.items[k])} if k in self.items else {}

    def delete_item(self, Key):
        self.items.pop(self._k(Key), None)

    def query(self, **_kw):
        return {"Items": [copy.deepcopy(v) for v in self.items.values()]}

    def scan(self, **_kw):
        return self.query()

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, ExpressionAttributeNames=None, **_kw):
        names = ExpressionAttributeNames or {}
        it = self.items.setdefault(self._k(Key), dict(Key))
        expr = UpdateExpression.strip()
        if expr.upper().startswith("SET "):
            for part in expr[4:].split(","):
                lhs, rhs = [x.strip() for x in part.split("=")]
                it[names.get(lhs, lhs)] = copy.deepcopy((ExpressionAttributeValues or {})[rhs])


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.text = json.dumps(data)[:300]

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def install_world(folder: Path, script: dict) -> dict:
    """Patch every table and outside service the planning chat touches."""
    import agent.tools as tools
    import api.deps as deps
    import api.services.planning_chat as PC
    import api.services.planning_live as PL

    before = json.loads((folder / "before.json").read_text(encoding="utf-8"))
    proj = before["project"]
    pid = proj["projectId"]
    W = {"pid": pid, "projects": Table("projectId", [proj]), "tasks": Table("taskId", before.get("tasks")),
         "insights": Table("insightId", before.get("insights")), "notifications": Table("notificationId"),
         "planning": Table("itemId"), "attachments": [], "notified": []}
    tools.projects_table, tools.tasks_table = W["projects"], W["tasks"]
    tools.insights_table, tools.notifications_table = W["insights"], W["notifications"]
    deps.attachments_table = Table("attachmentId")
    PC._table = W["planning"]
    PC.TODAY = script.get("today", "")
    PC._access = lambda uid, email, p: copy.deepcopy(W["projects"].items[pid])

    def store(proj_, uid, email, name, text, raw=None, ext="txt", content_type="text/plain"):
        W["attachments"].append({"fileName": name, "fullText": text, "createdAt": datetime.utcnow().isoformat(),
                                 "extension": ext})
    PC._store_source = store

    def snapshot(project_id, new_sources=(), read_all=False):
        names = {n for n, _t in new_sources}
        atts = [a for a in W["attachments"] if a["fileName"] not in names]
        atts += [{"fileName": n, "fullText": t, "createdAt": datetime.utcnow().isoformat()} for n, t in new_sources]
        return {"project": copy.deepcopy(W["projects"].items[pid]),
                "tasks": [copy.deepcopy(t) for t in W["tasks"].items.values()],
                "insights": [copy.deepcopy(i) for i in W["insights"].items.values() if i.get("type") == "note"],
                "attachments": atts}
    PL.snapshot = snapshot
    PL._log_to_s3 = lambda *a, **k: ""
    PL._notify = lambda *a, **k: W["notified"].append(a)

    fx = (script.get("fixtures") or {}).get("trello_api")
    if fx:
        import api.services.trello as TS
        raw = json.loads((folder / fx).read_text(encoding="utf-8"))
        by_path = {k.split(" ", 1)[1].split("?")[0]: v for k, v in raw.items() if k.startswith("GET ")}
        TS._creds = lambda uid: {"key": "k", "token": "t"}

        def fake_request(method, url, **kw):
            path = urlparse(url).path
            return FakeResponse(by_path[path]) if path in by_path else FakeResponse({"error": "not in fixture"}, 404)
        TS.requests.request = fake_request
    return W


# ── grading ─────────────────────────────────────────────────────────────────

def _as_list(v):
    return v if isinstance(v, list) else [v]


def _field_ok(actual, expected) -> bool:
    for e in _as_list(expected):
        a, e2 = norm(str(actual or "")), norm(str(e or ""))
        if a == e2 or (e2 and e2 in a) or (a and len(a) > 2 and a in e2):
            return True
    return False


def _task_match(task_id: str, expected) -> bool:
    return any((task_id or "").startswith(p) for p in _as_list(expected))


def _op_matches(o: dict, spec: dict, texts: dict) -> bool:
    if spec.get("op") and o.get("op") not in _as_list(spec["op"]):
        return False
    if spec.get("task") and not _task_match(o.get("task_id", ""), spec["task"]):
        return False
    for f, v in (spec.get("fields") or {}).items():
        if not _field_ok(o.get(f), v):
            return False
    return True


def _proposal_dict(item):
    if not item:
        return None
    import api.services.planning_chat as PC
    return PC._plain(PC._loads(item.get("proposal")) or {})


def grade(expect: dict, obs: dict) -> list:
    """Failed checks (empty = PASS)."""
    fails = []
    ops = obs["ops"]
    texts = obs["task_texts"]
    creates = [o for o in ops if o.get("op") == "create"]
    notes_txt = " | ".join(n.get("text", "") for n in obs["notes"])
    asks_txt = " | ".join(obs["questions"]) + " | " + obs["answer"]

    def need(cond, msg):
        if not cond:
            fails.append(msg)

    if "intent" in expect:
        need(obs["intent"] in _as_list(expect["intent"]), f"intent {obs['intent']!r}, expected {expect['intent']}")
    if "proposal" in expect:
        ok_states = _as_list(expect["proposal"])
        if "none" in ok_states:
            ok_states = ok_states + ["unchanged"]
        need(obs["proposal_state"] in ok_states, f"proposal {obs['proposal_state']!r}, expected {expect['proposal']}")
    if "applied" in expect:
        need(obs["applied"] == expect["applied"], f"applied={obs['applied']}, expected {expect['applied']}")
    for spec in expect.get("ops", []):
        need(any(_op_matches(o, spec, texts) for o in ops), f"missing op {spec}")
    for spec in expect.get("ops_absent", []):
        need(not any(_op_matches(o, spec, texts) for o in ops), f"forbidden op present {spec}")
    if expect.get("ops_any"):
        need(any(_op_matches(o, s, texts) for s in expect["ops_any"] for o in ops), f"none of {expect['ops_any']}")
    for spec in expect.get("creates", []):
        need(any(_matches(o.get("text", ""), spec["text"]) and _op_matches(o, {"fields": spec.get("fields")}, texts)
                 for o in creates), f"missing create {spec}")
    for groups in [[g] for g in expect.get("creates_absent", [])]:
        need(not any(_matches(o.get("text", ""), groups) for o in creates), f"forbidden create {groups}")
    for g in expect.get("creates_or_notes", []):
        need(any(_matches(o.get("text", ""), [g]) for o in creates) or _matches(notes_txt, [g]),
             f"neither create nor note mentions {g}")
    for spec in expect.get("creates_or_updates", []):
        need(any((_matches(o.get("text") or texts.get(o.get("task_id"), ""), spec["text"]))
                 and _op_matches(o, {"fields": spec.get("fields")}, texts)
                 for o in ops if o.get("op") in ("create", "update")), f"missing create/update {spec}")
    if expect.get("notes_mentions"):
        need(_matches(notes_txt, expect["notes_mentions"]), f"notes do not mention {expect['notes_mentions']}")
    for key, text in (("asks", asks_txt), ("answer_mentions", obs["answer"])):
        if expect.get(key):
            hits = sum(1 for g in expect[key] if _matches(text, [g]))
            need(hits >= expect.get(f"{key}_min", 1), f"{key}: {hits} of {expect[key]}")
    if "decision" in expect:
        d, e = obs["decision"] or {}, expect["decision"]
        need(d.get("kind") == e["kind"], f"decision {d.get('kind')!r}, expected {e['kind']!r}")
        excl = e.get("exclude")
        if excl and d.get("kind") == "approve_some":
            dops = obs["decided_ops"]
            approved = set(d.get("op_indexes") or [])
            for i, o in enumerate(dops, 1):
                hit = (o.get("op") in excl.get("op", [])) or _task_match(o.get("task_id", ""), excl.get("task", []))
                need((i in approved) != hit, f"op {i} {o.get('op')} {o.get('task_id')}: approved={i in approved}")
    for tid, fields in (expect.get("plan_state") or {}).items():
        t = next((t for k, t in obs["tasks"].items() if k.startswith(tid)), None)
        if t is None:
            fails.append(f"plan_state: task {tid} not found")
            continue
        for f, v in fields.items():
            vals = _as_list(v)
            ok = (t.get(f) or "") in vals if "" in vals else _field_ok(t.get(f), v)
            need(ok, f"plan_state {tid}.{f}={t.get(f)!r}, expected {v}")
    if "handoff" in expect:
        h = obs["handoff"]
        if not h:
            fails.append("no handoff to the assistant chat (external actions not implemented)")
        else:
            e = expect["handoff"]
            blob = norm(json.dumps(h, ensure_ascii=False))
            if e.get("to"):
                need(any(norm(x) in blob for x in e["to"]), f"handoff recipient not in {e['to']}")
            if e.get("mentions"):
                hits = sum(1 for g in e["mentions"] if _matches(blob, [g]))
                need(hits >= e.get("mentions_min", 1), f"handoff mentions {hits} of {e['mentions']}")
    if expect.get("depends_on_min"):
        dm = expect["depends_on_min"]
        n = sum(1 for o in ops if any(_task_match(d, dm["task"]) for d in o.get("depends_on") or []))
        need(n >= dm["count"], f"{n} ops depend on {dm['task']}, expected >= {dm['count']}")
    if expect.get("depends_absent"):
        da = expect["depends_absent"]
        bad = [o.get("task_id") for o in ops if _task_match(o.get("task_id", ""), da["on"])
               and any(_task_match(d, da["task"]) for d in o.get("depends_on") or [])]
        need(not bad, f"tasks {bad} should not depend on {da['task']}")
    if expect.get("no_due_date_on_creates"):
        need(not any(o.get("due_date") for o in creates), "creates carry due dates")
    for spec in expect.get("dates_any", []):
        need(any(o.get("op") in ("update", "create") and _task_match(o.get("task_id", ""), spec["task"])
                 and o.get("due_date") == spec["due_date"] for o in ops), f"no due date {spec['due_date']} on {spec['task']}")
    return fails


# ── running ─────────────────────────────────────────────────────────────────

def _pending(W):
    import api.services.planning_chat as PC
    item = PC.pending_proposal(W["pid"])
    return item


class EmptyLLM:
    """--dry: every LLM call answers an empty structure. Exercises the
    plumbing (tables, Trello fixtures, documents, grading) at zero cost."""
    def with_structured_output(self, schema, **_kw):
        class R:
            def invoke(self, _m):
                return schema()
        return R()


def run(name: str, upto: int = 0, dry: bool = False) -> dict:
    import api.services.planning_chat as PC
    folder = CONV / name
    script = json.loads((folder / "script.json").read_text(encoding="utf-8"))
    if script.get("kind") in ("member", "consolidation"):
        from tests.planning import updates_runner as UR
        fn = UR.run_member if script["kind"] == "member" else UR.run_consolidation
        return fn(name, folder, script, dry)
    W = install_world(folder, script)
    email = script["leader"]["email"]
    turns, transcript = script["turns"][: upto or None], []
    t_all = time.time()
    for i, turn in enumerate(turns, 1):
        before_item = _pending(W)
        before_prop = _proposal_dict(before_item)
        version0 = int(W["projects"].items[W["pid"]].get("planVersion") or 0)
        ctx = turn.get("context") or {}
        file = None
        if ctx.get("file"):
            fp = folder / ctx["file"]
            ctype = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     ".md": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf"}.get(fp.suffix, "")
            file = (fp.read_bytes(), fp.name, ctype)
        msg_ids = set(W["planning"].items)
        t0, job, error = time.time(), {}, ""
        try:
            out = PC.start_message(LEADER_UID, email, W["pid"], "\n".join(turn["messages"]), file=file,
                                   include_trello=bool(ctx.get("trello")), run_async=False,
                                   llm=EmptyLLM() if dry else None)
            job = PC._JOBS.get(out["jobId"], {})
            error = job.get("error", "") if job.get("status") == "error" else ""
        except Exception as e:
            error = f"{type(e).__name__}: {getattr(e, 'detail', e)}"
            traceback.print_exc()
        after_item = _pending(W)
        after_prop = _proposal_dict(after_item)
        version1 = int(W["projects"].items[W["pid"]].get("planVersion") or 0)
        new_items = [W["planning"].items[k] for k in W["planning"].items if k not in msg_ids]
        answer = " ".join(m.get("content", "") for m in new_items if m.get("role") == "assistant")
        if after_item is None:
            state = "none"
        elif before_item and after_item["proposalId"] == before_item["proposalId"]:
            state = "edited" if after_prop != before_prop else "unchanged"
        else:
            state = "new"
        decision = job.get("decision")
        decided = _proposal_dict(W["planning"].items.get(f"prop#{decision['proposalId']}")) if decision else None
        cur = after_prop or decided or {}
        intent = (job.get("intent") or {}).get("kind", "") or ("decide" if decision else "")
        obs = {"intent": "decide" if decision else intent, "proposal_state": state, "applied": version1 > version0,
               "ops": cur.get("operations") or [], "notes": cur.get("notes") or [],
               "questions": [q.get("text", "") for q in cur.get("questions") or []], "answer": answer,
               "decision": decision, "decided_ops": (decided or {}).get("operations") or [],
               "tasks": {k: v for k, v in W["tasks"].items.items()},
               "task_texts": {k: v.get("text", "") for k, v in W["tasks"].items.items()},
               "handoff": job.get("handoff")}
        fails = grade(turn.get("expect") or {}, obs) if not error else [f"crashed: {error}"]
        transcript.append({"turn": i, "messages": turn["messages"], "context": ctx, "why": turn.get("why", ""),
                           "seconds": round(time.time() - t0, 1), "intent": obs["intent"],
                           "proposal_state": state, "applied": obs["applied"], "decision": decision,
                           "answer": answer, "ops": obs["ops"], "notes": obs["notes"], "questions": obs["questions"],
                           "nodes": job.get("trace") or job.get("steps"), "fails": fails})
        print(f"turn {i}: {'PASS' if not fails else 'FAIL'} ({transcript[-1]['seconds']} s) {fails[:2]}", flush=True)
    passed = sum(1 for t in transcript if not t["fails"])
    result = {"conversation": name, "turns": len(transcript), "passed": passed,
              "score": round(passed / len(transcript), 3) if transcript else 0, "seconds": round(time.time() - t_all, 1)}
    _write(folder, result, transcript, script)
    return result


def _short(o, texts):
    t = o.get("text") or texts.get(o.get("task_id"), "") or o.get("task_id", "")
    extra = ", ".join(f"{k}={o[k]}" for k in ("status", "assigned_to", "due_date", "blocked_reason", "duplicate_of")
                      if o.get(k))
    deps = f" deps={o['depends_on']}" if o.get("depends_on") else ""
    return f"{o.get('op')} [{o.get('task_id', '')[:14]}] {t[:70]}" + (f" ({extra})" if extra else "") + deps


def _write(folder: Path, result: dict, transcript: list, script: dict):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    L = [f"# {result['conversation']} — {result['passed']}/{result['turns']} turnos OK ({result['seconds']} s)", "",
         f"_{script.get('title', '')}_ · hoy {script.get('today')} · dueño {script['leader']['name']}", ""]
    for t in transcript:
        L += [f"## Turno {t['turn']} — {'✅' if not t['fails'] else '❌'}  ({t['seconds']} s)", ""]
        L += [f"> {m}" for m in t["messages"]]
        if t["context"]:
            L.append(f"> _contexto: {t['context']}_")
        if t["why"]:
            L.append(f"\n_{t['why']}_")
        L += ["", f"- intención: **{t['intent'] or '-'}** · propuesta: **{t['proposal_state']}** · aplicado: {t['applied']}"
              + (f" · decisión: {t['decision']}" if t["decision"] else ""),
              f"- nodos: {' → '.join(t['nodes'] or [])}", "", "**Respuesta:** " + (t["answer"][:700] or "(ninguna)"), ""]
        if t["ops"]:
            L += ["**Propuesta:**"] + [f"- {_short(o, {})}" for o in t["ops"][:25]] + [""]
        if t["questions"]:
            L += ["**Preguntas:**"] + [f"- {q}" for q in t["questions"]] + [""]
        if t["notes"]:
            L += ["**Notas:**"] + [f"- {n.get('text', '')[:120]}" for n in t["notes"][:8]] + [""]
        if t["fails"]:
            L += ["**Falló:**"] + [f"- {f}" for f in t["fails"]] + [""]
    report = "\n".join(L)
    for d in (folder / "runs" / stamp, folder / "runs" / "latest"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.md").write_text(report, encoding="utf-8")
        (d / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2, default=str),
                                           encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("--upto", type=int, default=0, help="run only the first N turns")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry", action="store_true", help="no LLM: check the plumbing only")
    a = ap.parse_args()
    if a.list or not a.name:
        for d in sorted(p for p in CONV.iterdir() if (p / "script.json").exists()):
            s = json.loads((d / "script.json").read_text(encoding="utf-8"))
            n = len(s.get("turns") or s.get("inputs") or [])
            print(f"{d.name} [{s.get('kind', 'replan')}]: {n} pasos — {s.get('title', '')}")
        return
    print(json.dumps(run(a.name, a.upto, a.dry), ensure_ascii=False))


if __name__ == "__main__":
    main()
