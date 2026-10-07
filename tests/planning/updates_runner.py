"""Runner for the UPDATE loop (kind: member / consolidation), the core of the
product. Called by run_conversation.run() according to script["kind"].

    member         a member gives their update in the app chat. Graded per turn
                   (does the AI ask what matters, at most N questions, close
                   with a summary) and at the end (the recorded items: type,
                   task, certainty, owner, date...; the plan must not change).
    consolidation  several member conversations on the same project (+ the
                   Trello board) -> consolidation -> digest + proposal; checks
                   urgent alerts, contradictions, guard rules, the questions
                   for the owner; optionally approves and checks what would be
                   written to Trello.
"""
import copy
import json
import re
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from agent.planning.sources import norm
from tests.planning.evaluate import _matches


def _has(text: str, groups) -> bool:
    """Every group must match; a group matches when ANY of its words does
    (the scripts write [["microsoft", "outlook"]] meaning microsoft OR outlook)."""
    return all(_matches(text, [[w] for w in _as_list(g)]) for g in groups)


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _field_ok(actual, expected) -> bool:
    for e in _as_list(expected):
        a, e2 = norm(str(actual or "")), norm(str(e or ""))
        if a == e2 or (e2 and e2 in a) or (a and len(a) > 2 and a in e2):
            return True
    return False


def _task_ok(task_id, expected) -> bool:
    return any((task_id or "").startswith(p) for p in _as_list(expected))


def questions_in(text: str) -> int:
    """Questions in a reply: '¿...?' pairs, else sentences ending in '?'."""
    return max(len(re.findall(r"¿", text or "")), len(re.findall(r"\?", text or "")))


class DryLLM:
    """--dry: answers without calling a model (plumbing only)."""
    def with_structured_output(self, schema, **_kw):
        class R:
            def invoke(self, _m):
                try:
                    return schema(reply="(dry) ¿algo más?")
                except Exception:
                    return schema()
        return R()


def setup_world(folder: Path, script: dict, trello_fixture: str = ""):
    """The shared fake world + the update-loop modules pinned to the script's day."""
    from tests.planning.run_conversation import FakeResponse, install_world
    import api.services.checkin as C
    import api.services.member_updates as MU
    import api.services.update_reminders as UR
    import api.services.updates_consolidation as UC
    W = install_world(folder, {k: v for k, v in script.items() if k != "fixtures"})
    UR._send = lambda proj, person, prep: W.setdefault("mails", []).append((person.get("email"), prep)) or {"status": "sent"}
    pid = W["pid"]
    proj = W["projects"].items[pid]
    MU.TODAY = UC.TODAY = script.get("today", "")
    owner = script.get("owner") or next((p for p in proj.get("participants") or [] if isinstance(p, dict)
                                         and p.get("email")), {})
    C._owner_email = lambda uid: (owner.get("email") or "").lower()
    C._project_tasks = lambda p: [copy.deepcopy(t) for t in W["tasks"].items.values()]
    C._notify_owner_outside = lambda pr, title, lines: W["notified"].append(("outside", title, "\n".join(lines)))
    import api.services.planning_live as PL
    PL._notify = lambda owner_uid, pr, title, msg, kind: W["notified"].append((kind, title, msg))
    W["trello_writes"] = []
    if trello_fixture:
        import agent.tools.trello as tool_trello
        import api.services.trello as TS
        raw = json.loads((folder / trello_fixture).read_text(encoding="utf-8"))
        by_path = {k.split(" ", 1)[1].split("?")[0]: v for k, v in raw.items() if k.startswith("GET ")}
        TS._creds = lambda uid: {"key": "k", "token": "t"}
        tool_trello.tasks_table = W["tasks"]

        def fake_request(method, url, **kw):
            path = urlparse(url).path
            if method == "GET":
                return FakeResponse(by_path[path]) if path in by_path else FakeResponse({"error": "not in fixture"}, 404)
            params = {k: v for k, v in (kw.get("params") or {}).items() if k not in ("key", "token")}
            W["trello_writes"].append({"method": method, "path": path, "params": params})
            cid = path.rsplit("/", 1)[-1] if method == "PUT" else f"new{len(W['trello_writes'])}"
            return FakeResponse({"id": cid, "shortUrl": f"https://trello.com/c/{cid}"})
        TS.requests.request = fake_request
    return W, owner


# ── member ──────────────────────────────────────────────────────────────────

def grade_turn(expect: dict, reply: str, finished: bool) -> list:
    fails = []
    if expect.get("ai_asks"):
        hits = sum(1 for g in expect["ai_asks"] if _has(reply, [g]))
        if hits < expect.get("ai_asks_min", len(expect["ai_asks"])):
            fails.append(f"asks about {hits} of {expect['ai_asks']}")
    if "ai_questions_max" in expect and questions_in(reply) > expect["ai_questions_max"]:
        fails.append(f"{questions_in(reply)} questions, max {expect['ai_questions_max']}")
    if "ai_closes" in expect and bool(finished) != bool(expect["ai_closes"]):
        fails.append(f"finished={finished}, expected {expect['ai_closes']}")
    if expect.get("ai_summary_mentions"):
        hits = sum(1 for g in expect["ai_summary_mentions"] if _has(reply, [g]))
        if hits < expect.get("ai_summary_min", len(expect["ai_summary_mentions"])):
            fails.append(f"summary mentions {hits} of {expect['ai_summary_mentions']}")
    if not (reply or "").strip():
        fails.append("empty reply")
    return fails


def _item_ok(i: dict, spec: dict) -> bool:
    if spec.get("type") and i.get("type") not in _as_list(spec["type"]):
        return False
    if spec.get("task") and not _task_ok(i.get("task_id"), spec["task"]):
        return False
    if spec.get("mentions") and not _has(f"{i.get('text', '')} {i.get('quote', '')} {i.get('owner', '')}",
                                         spec["mentions"]):
        return False
    for f in ("owner", "certainty", "about_person"):
        if spec.get(f) and not _field_ok(i.get(f), spec[f]):
            return False
    if spec.get("due_date") and i.get("due_date") not in _as_list(spec["due_date"]):
        return False
    if spec.get("depends_on") and not _task_ok(i.get("depends_on"), spec["depends_on"]):
        return False
    for f in ("urgent", "decision_for_owner"):
        if f in spec and bool(i.get(f)) != bool(spec[f]):
            return False
    return True


def grade_report(expect: dict, items: list, plan_changed: bool, finished: bool) -> list:
    fails = []
    for spec in expect.get("items", []):
        if not any(_item_ok(i, spec) for i in items):
            fails.append(f"missing item {spec}")
    for spec in expect.get("items_absent", []):
        bad = [i for i in items if _item_ok(i, spec)]
        if bad:
            fails.append(f"forbidden item {spec}: {bad[0].get('text', '')[:80]}")
    if expect.get("plan_unchanged") and plan_changed:
        fails.append("the plan changed during the member's update")
    if not finished:
        fails.append("the update was never closed")
    return fails


def member_conversation(W, folder: Path, script: dict, dry: bool = False) -> dict:
    """Run one member conversation in world W. Returns transcript + final items."""
    import api.services.member_updates as MU
    m = script["member"]
    uid = "member-" + norm(m.get("name", "x")).replace(" ", "-")
    transcript, items, finished = [], [], False
    # 'hora de actualizar': the AI prepares the questions (email + chat opening)
    t0 = time.time()
    try:
        import api.services.checkin as C
        import api.services.update_reminders as UR
        proj = W["projects"].items[W["pid"]]
        mine = next(((p, ts) for p, ts in C.assignments(proj, C._project_tasks(W["pid"]))
                     if (m.get("email") and p.get("email") == m["email"].lower())
                     or norm(p.get("name", "")) == norm(m.get("name", ""))), None)
        prep = UR.opening_for(proj, mine[0], mine[1], llm=DryLLM() if dry else None) if mine else None
        if prep:
            ids = list(W["tasks"].items)
            pf = [] if 2 <= len(prep["questions"]) <= 3 else [f"{len(prep['questions'])} prepared questions"]
            pf += [f"task id shown: {q}" for q in prep["questions"] if any(i in q for i in ids)]
            for g in (script.get("expect_prep") or {}).get("mentions", []):
                if not _has(" ".join(prep["questions"]), [g]):
                    pf.append(f"prepared questions do not mention {g}")
            transcript.append({"turn": "correo", "messages": [], "seconds": round(time.time() - t0, 1),
                               "answer": f"Asunto: {prep.get('subject')}\n" + UR.email_text(proj, prep), "fails": pf})
    except Exception as e:
        traceback.print_exc()
        transcript.append({"turn": "correo", "messages": [], "seconds": 0, "answer": "", "fails": [f"crashed: {e}"]})
    for i, turn in enumerate(script["turns"], 1):
        t0, error = time.time(), ""
        n_before = len(W["notified"])
        try:
            out = MU.converse(uid, (m.get("email") or "").lower(), W["pid"], turn["messages"], m.get("name", ""),
                              llm=DryLLM() if dry else None)
        except Exception as e:
            traceback.print_exc()
            error, out = f"{type(e).__name__}: {getattr(e, 'detail', e)}", {"reply": "", "finished": False, "items": items}
        items, finished = out.get("items") or items, bool(out.get("finished"))
        fails = [f"crashed: {error}"] if error else grade_turn(turn.get("expect") or {}, out.get("reply", ""), finished)
        transcript.append({"turn": i, "messages": turn["messages"], "why": turn.get("why", ""),
                           "seconds": round(time.time() - t0, 1), "answer": out.get("reply", ""), "finished": finished,
                           "items": items, "alerts": W["notified"][n_before:], "fails": fails})
        print(f"  {script.get('member', {}).get('name')} turn {i}: {'PASS' if not fails else 'FAIL'} "
              f"({transcript[-1]['seconds']} s) {fails[:2]}", flush=True)
        if finished:
            break
    return {"transcript": transcript, "items": items, "finished": finished}


def _plan_fingerprint(W) -> str:
    keep = ("text", "status", "dueDate", "assignedTo", "dependsOn", "blockedReason")
    return json.dumps({k: {f: v.get(f) for f in keep} for k, v in W["tasks"].items.items()}, sort_keys=True, default=str)


def run_member(name: str, folder: Path, script: dict, dry: bool = False) -> dict:
    W, _owner = setup_world(folder, script)
    plan0 = _plan_fingerprint(W)
    t_all = time.time()
    r = member_conversation(W, folder, script, dry)
    props = [k for k in W["planning"].items if k.startswith("prop#")]
    changed = _plan_fingerprint(W) != plan0 or bool(props)
    rep = grade_report(script.get("expect_report") or {}, r["items"], changed, r["finished"])
    transcript = r["transcript"] + [{"turn": "informe", "messages": [], "seconds": 0, "answer": "",
                                     "items": r["items"], "fails": rep}]
    print(f"  report: {'PASS' if not rep else 'FAIL'} {rep[:3]}", flush=True)
    passed = sum(1 for t in transcript if not t["fails"])
    result = {"conversation": name, "kind": "member", "turns": len(transcript), "passed": passed,
              "score": round(passed / len(transcript), 3), "report_passed": not rep,
              "seconds": round(time.time() - t_all, 1)}
    write(folder, result, transcript, script)
    return result


# ── consolidation ───────────────────────────────────────────────────────────

def grade_consolidation(expect: dict, W, out: dict, proposal: dict, alerts_after: dict, plan_changed: bool) -> list:
    fails = []
    ops, questions = proposal.get("operations") or [], [q.get("text", "") for q in proposal.get("questions") or []]
    digest = out.get("digest", "")
    for u in expect.get("urgent_to_owner", []):
        alerts = alerts_after.get(u.get("after"), []) if u.get("after") else [a for v in alerts_after.values() for a in v]
        blob = " ".join(f"{a[1]} {a[2]}" for a in alerts if a[0] in ("update_urgent", "outside"))
        if not _has(blob, u["mentions"]):
            fails.append(f"no urgent alert to the owner about {u['mentions']}" + (f" after {u['after']}" if u.get("after") else ""))
    contra = (out.get("build") or {}).get("contradictions") or []
    for c in expect.get("contradictions", []):
        hit = [x for x in contra if _task_ok(x["task_id"], c["task"])]
        if not hit:
            fails.append(f"contradiction on {c['task']} not detected")
        elif not _has(hit[0]["text"], c.get("mentions", [])):
            fails.append(f"contradiction on {c['task']} does not name {c['mentions']}")
    p = expect.get("proposal") or {}
    for spec in p.get("ops", []):
        if not any(o.get("op") in _as_list(spec["op"]) and _task_ok(o.get("task_id"), spec["task"]) for o in ops):
            fails.append(f"missing op {spec}")
    for spec in p.get("ops_absent", []):
        if any(o.get("op") in _as_list(spec["op"]) and _task_ok(o.get("task_id"), spec["task"]) for o in ops):
            fails.append(f"forbidden op {spec}")
    for d in p.get("depends", []):
        if not any(_task_ok(o.get("task_id"), d["task"]) and any(_task_ok(x, d["on"]) for x in o.get("depends_on") or [])
                   for o in ops):
            fails.append(f"missing dependency {d['task']} -> {d['on']}")
    for groups in p.get("decisions_for_owner", []):
        if not any(_has(q, groups) for q in questions):
            fails.append(f"no question for the owner about {groups}")
    dg = expect.get("digest") or {}
    if dg.get("mentions"):
        hits = sum(1 for g in dg["mentions"] if _has(digest, [g]))
        if hits < dg.get("mentions_min", len(dg["mentions"])):
            fails.append(f"digest mentions {hits} of {dg['mentions']}")
    if "not_reported" in dg and sorted(norm(x) for x in (out.get("build") or {}).get("not_reported") or []) != \
            sorted(norm(x) for x in dg["not_reported"]):
        fails.append(f"not reported {out.get('build', {}).get('not_reported')}, expected {dg['not_reported']}")
    if expect.get("plan_unchanged_until_approved") and plan_changed:
        fails.append("the plan changed before the owner approved")
    return fails


def grade_trello(expect: dict, writes: list, decided: dict) -> list:
    fails = []
    tr = decided.get("trello") or {}
    for w in expect.get("writes", []):
        ok = any(x["method"] == w.get("method", x["method"]) and x["path"].endswith(w.get("card", x["path"]))
                 and all(str(x["params"].get(k)) == str(v) for k, v in (w.get("params") or {}).items())
                 and all(k in x["params"] for k in w.get("param_keys") or []) for x in writes)
        if not ok:
            fails.append(f"missing Trello write {w}")
    for w in expect.get("writes_absent", []):
        if any(x["method"] == w.get("method", x["method"]) and x["path"].endswith(w.get("card", "")) and
               all(k in x["params"] for k in w.get("param_keys") or []) for x in writes):
            fails.append(f"forbidden Trello write {w}")
    if expect.get("status") and tr.get("status") not in _as_list(expect["status"]):
        fails.append(f"trello status {tr.get('status')}, expected {expect['status']}")
    return fails


def run_consolidation(name: str, folder: Path, script: dict, dry: bool = False) -> dict:
    import api.services.planning_chat as PC
    import api.services.updates_consolidation as UC
    trello_fx = next((i["trello_api"] for i in script.get("inputs", []) if i.get("trello_api")), "")
    W, owner = setup_world(folder, script, trello_fx)
    proj = W["projects"].items[W["pid"]]
    proj.update(script.get("project_patch") or {})
    for tid, card in (script.get("trello_links") or {}).items():
        if tid in W["tasks"].items:
            W["tasks"].items[tid]["trelloCardId"] = card
    plan0 = _plan_fingerprint(W)
    t_all = time.time()
    transcript, alerts_after = [], {}
    for inp in script.get("inputs", []):
        conv = inp.get("member_conversation")
        if not conv:
            continue
        sub = json.loads((folder.parent / conv / "script.json").read_text(encoding="utf-8"))
        n0 = len(W["notified"])
        r = member_conversation(W, folder.parent / conv, sub, dry)
        alerts_after[conv] = W["notified"][n0:]
        rep = grade_report({k: v for k, v in (sub.get("expect_report") or {}).items() if k != "plan_unchanged"},
                           r["items"], False, r["finished"])
        transcript.append({"turn": conv, "messages": [m for t in sub["turns"] for m in t["messages"]],
                           "seconds": sum(t["seconds"] for t in r["transcript"]),
                           "answer": r["transcript"][-1]["answer"] if r["transcript"] else "", "items": r["items"],
                           "alerts": alerts_after[conv],
                           "fails": [f for t in r["transcript"] for f in t["fails"]] + rep, "member_turns": r["transcript"]})
    plan_changed = _plan_fingerprint(W) != plan0
    t0 = time.time()
    try:
        out = UC.consolidate(W["pid"], actor="cron", send_external=True)
    except Exception as e:
        traceback.print_exc()
        out = {"status": "error", "digest": "", "error": str(e)}
    item = W["planning"].items.get(f"prop#{out.get('proposalId')}") if out.get("proposalId") else None
    proposal = PC._plain(PC._loads(item.get("proposal")) or {}) if item else {}
    fails = grade_consolidation(script.get("expect") or {}, W, out, proposal, alerts_after, plan_changed)
    if out.get("status") == "error":
        fails.insert(0, f"crashed: {out.get('error')}")
    transcript.append({"turn": "consolidación", "messages": [], "seconds": round(time.time() - t0, 1),
                       "answer": out.get("digest", ""), "ops": proposal.get("operations") or [],
                       "questions": [q.get("text", "") for q in proposal.get("questions") or []],
                       "notes": proposal.get("notes") or [], "fails": fails})
    print(f"  consolidation: {'PASS' if not fails else 'FAIL'} {fails[:3]}", flush=True)
    if script.get("approve") and item:
        W["trello_writes"].clear()
        try:
            decided = PC.decide(proj.get("userId", ""), (owner.get("email") or ""), W["pid"], item["proposalId"],
                                "approve_all")
            tf = grade_trello(script.get("expect_trello") or {}, W["trello_writes"], decided)
        except Exception as e:
            traceback.print_exc()
            decided, tf = {}, [f"crashed: {getattr(e, 'detail', e)}"]
        transcript.append({"turn": "aprobación + Trello", "messages": ["(el dueño aprueba todo)"], "seconds": 0,
                           "answer": json.dumps(decided.get("trello") or {}, ensure_ascii=False)[:700],
                           "trello_writes": list(W["trello_writes"]), "fails": tf})
        print(f"  approve + Trello: {'PASS' if not tf else 'FAIL'} {tf[:3]}", flush=True)
    passed = sum(1 for t in transcript if not t["fails"])
    result = {"conversation": name, "kind": "consolidation", "turns": len(transcript), "passed": passed,
              "score": round(passed / len(transcript), 3), "seconds": round(time.time() - t_all, 1)}
    write(folder, result, transcript, script)
    return result


# ── report ──────────────────────────────────────────────────────────────────

def _item_line(i: dict) -> str:
    extra = ", ".join(f"{k}={i[k]}" for k in ("task_id", "certainty", "owner", "due_date", "depends_on", "about_person")
                      if i.get(k))
    flags = (" URGENTE" if i.get("urgent") else "") + (" [decide el dueño]" if i.get("decision_for_owner") else "")
    return f"{i.get('type')}: {i.get('text', '')[:100]}" + (f" ({extra})" if extra else "") + flags


def write(folder: Path, result: dict, transcript: list, script: dict):
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    who = (script.get("member") or script.get("owner") or {}).get("name", "")
    L = [f"# {result['conversation']} — {result['passed']}/{result['turns']} OK ({result['seconds']} s)", "",
         f"_{script.get('title', '')}_ · hoy {script.get('today')} · {result['kind']} · {who}", ""]
    for t in transcript:
        L += [f"## {t['turn']} — {'✅' if not t['fails'] else '❌'}  ({t.get('seconds', 0)} s)", ""]
        L += [f"> {m}" for m in t.get("messages") or []]
        if t.get("why"):
            L.append(f"\n_{t['why']}_")
        for mt in t.get("member_turns") or []:
            L += [f"- **miembro:** {' / '.join(mt['messages'])}", f"  **IA:** {mt['answer'][:400]}"]
        if t.get("answer"):
            L += ["", "**IA:** " + t["answer"][:1500].replace("\n", "  \n"), ""]
        if t.get("items") and t["turn"] in ("informe",) or t.get("member_turns"):
            L += ["**Items registrados:**"] + [f"- {_item_line(i)}" for i in t.get("items") or []] + [""]
        if t.get("alerts"):
            L += ["**Avisos al dueño:**"] + [f"- {a[0]}: {a[1]}" for a in t["alerts"]] + [""]
        if t.get("ops"):
            L += ["**Propuesta:**"] + [f"- {o.get('op')} [{o.get('task_id', '')}] {o.get('reason', '')[:100]}"
                                       + (f" deps={o['depends_on']}" if o.get("depends_on") else "") for o in t["ops"]] + [""]
        if t.get("questions"):
            L += ["**Preguntas para el dueño:**"] + [f"- {q}" for q in t["questions"]] + [""]
        if t.get("trello_writes"):
            L += ["**Escrituras en Trello:**"] + [f"- {w['method']} {w['path']} {w['params']}" for w in t["trello_writes"]] + [""]
        if t["fails"]:
            L += ["**Falló:**"] + [f"- {f}" for f in t["fails"]] + [""]
    report = "\n".join(L)
    for d in (folder / "runs" / stamp, folder / "runs" / "latest"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.md").write_text(report, encoding="utf-8")
        (d / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2, default=str),
                                           encoding="utf-8")
