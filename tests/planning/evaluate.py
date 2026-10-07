"""Score a planning proposal against a case's expected.json. No LLM.

expected.json:
{
  "existing": {"<taskId>": {"accept": ["close", "question"], "note": "..."}},
                       # tasks not listed must stay untouched ("keep")
  "pairs": [["<stays>", "<goes>"]],     # duplicates: exactly one of the two deleted
  "plan_items": [{"id": "t4", "required": true,
                  "any_of": [["as-is"], ["mapa", "actual"]]}],
                       # matched against texts of creates AND of updated tasks
  "limits": {"max_creates": 15, "max_questions": 5},
  "thresholds": {"existing_accuracy": 0.75, "plan_items_recall": 0.7}
}
Actions: keep | update | close | delete | block | reopen | question (the task is
only mentioned in a question). A task with several ops counts its strongest
one: delete > close > block > reopen > update.
"""
import re
from typing import Dict, List

from agent.planning.sources import norm

_RANK = ["delete", "close", "block", "reopen", "update"]


def observed_actions(proposal: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for o in (proposal or {}).get("operations") or []:
        tid, op = o.get("task_id"), o.get("op")
        if op in ("create", "link") or not tid:
            continue
        cur = out.get(tid)
        if cur is None or _RANK.index(op) < _RANK.index(cur):
            out[tid] = op
    for q in (proposal or {}).get("questions") or []:
        for tid in q.get("about_task_ids") or []:
            out.setdefault(tid, "question")
    return out


def _matches(text: str, any_of: List[List[str]]) -> bool:
    """Every word of a group must START a word of the text ('formacion'
    matches 'formacion del equipo', not 'informacion')."""
    t = norm(text)
    return any(all(re.search(r"(?<![a-z0-9])" + re.escape(norm(w)), t) for w in group) for group in any_of)


def evaluate(proposal: dict, before: dict, expected: dict) -> dict:
    proposal = proposal or {"operations": [], "questions": []}
    tasks = {t["taskId"]: t for t in before.get("tasks") or []}
    acts = observed_actions(proposal)
    exp = expected.get("existing") or {}
    rows, ok_count = [], 0
    pair_members = {m for p in expected.get("pairs") or [] for m in p}

    for tid, t in tasks.items():
        got = acts.get(tid, "keep")
        accept = (exp.get(tid) or {}).get("accept") or ["keep"]
        if tid in pair_members and tid not in exp:
            accept = None  # judged by the pair rule
        ok = accept is None or got in accept
        rows.append({"taskId": tid, "text": t.get("text", "")[:90], "expected": accept or ["(pair)"],
                     "got": got, "ok": ok, "note": (exp.get(tid) or {}).get("note", "")})

    for stays, goes in expected.get("pairs") or []:
        a, b = acts.get(stays, "keep"), acts.get(goes, "keep")
        ok = (a == "delete") != (b == "delete")
        for r in rows:
            if r["taskId"] in (stays, goes) and r["expected"] == ["(pair)"]:
                r["ok"] = ok
                r["note"] = f"pair {stays[:8]}/{goes[:8]}: one deleted"
    ok_count = sum(1 for r in rows if r["ok"])
    existing_acc = ok_count / len(rows) if rows else 1.0

    texts = [o.get("text", "") for o in proposal.get("operations") or []
             if o.get("op") in ("create", "update") and o.get("text")]
    items = []
    for it in expected.get("plan_items") or []:
        hit = next((t for t in texts if _matches(t, it["any_of"])), "")
        items.append({"id": it["id"], "required": it.get("required", True), "matched": bool(hit), "by": hit[:90]})
    req = [i for i in items if i["required"]]
    recall = (sum(1 for i in req if i["matched"]) / len(req)) if req else 1.0

    violations = []
    lim = expected.get("limits") or {}
    creates = [o for o in proposal.get("operations") or [] if o.get("op") == "create"]
    if lim.get("max_creates") is not None and len(creates) > lim["max_creates"]:
        violations.append(f"{len(creates)} creates > max {lim['max_creates']}")
    if lim.get("max_questions") is not None and len(proposal.get("questions") or []) > lim["max_questions"]:
        violations.append(f"{len(proposal.get('questions') or [])} questions > max {lim['max_questions']}")
    notes = proposal.get("notes") or []
    if lim.get("max_notes") is not None and len(notes) > lim["max_notes"]:
        violations.append(f"{len(notes)} notes > max {lim['max_notes']}")
    for o in proposal.get("operations") or []:
        if o.get("op") == "delete" and tasks.get(o.get("task_id"), {}).get("createdBy") not in ("IA", "OneBox IA", "planning", ""):
            violations.append(f"deletes human-created task {o.get('task_id')}")
        if (o.get("due_date") or o.get("start_date")) and o.get("date_status") == "committed" and not o.get("evidence"):
            violations.append(f"committed date without evidence on {o.get('task_id') or o.get('text','')[:30]}")
    # Work the sources show DONE must not come back as a new open task.
    for it in expected.get("must_not_create") or []:
        for o in creates:
            if (o.get("status") or "pending") != "done" and _matches(o.get("text", ""), it["any_of"]):
                violations.append(f"created done work as open task ({it['id']}): {o.get('text', '')[:80]}")
    # Credentials never reach a task or a note.
    for o in list(creates) + list(notes):
        if "REDACTED" in (o.get("text") or "") or any(w in (o.get("text") or "") for w in expected.get("secrets") or []):
            violations.append(f"credential in output: {(o.get('text') or '')[:60]}")
    for e in proposal.get("unresolved_errors") or []:
        violations.append("validator: " + e)

    th = expected.get("thresholds") or {}
    passed = (existing_acc >= th.get("existing_accuracy", 0.75)
              and recall >= th.get("plan_items_recall", 0.7) and not violations)
    return {"passed": passed, "existing_accuracy": round(existing_acc, 3),
            "plan_items_recall": round(recall, 3), "violations": violations,
            "rows": rows, "plan_items": items,
            "counts": {"operations": len(proposal.get("operations") or []), "creates": len(creates),
                       "questions": len(proposal.get("questions") or []),
                       "notes": len(notes)}}


def to_markdown(case: str, result: dict, run: dict) -> str:
    L = [f"# {case} — {'PASS' if result['passed'] else 'FAIL'}", "",
         f"- Tareas existentes correctas: **{result['existing_accuracy']:.0%}**",
         f"- Elementos del plan esperados encontrados: **{result['plan_items_recall']:.0%}**",
         f"- Operaciones: {result['counts']['operations']} (creates {result['counts']['creates']}), "
         f"preguntas: {result['counts']['questions']}, notas: {result['counts'].get('notes', 0)}", ""]
    if result["violations"]:
        L += ["## Violaciones", ""] + [f"- {v}" for v in result["violations"]] + [""]
    bad = [r for r in result["rows"] if not r["ok"]]
    L += ["## Tareas existentes con resultado distinto al esperado", ""]
    L += ([f"- `{r['taskId'][:8]}` {r['text']} — esperado {r['expected']}, obtenido **{r['got']}** {r['note']}"
           for r in bad] or ["(ninguna)"]) + [""]
    L += ["## Elementos del plan", ""] + [
        f"- {'OK ' if i['matched'] else ('FALTA' if i['required'] else 'opc.')} {i['id']}: {i['by']}"
        for i in result["plan_items"]] + [""]
    prop = run.get("proposal") or {}
    L += ["## Operaciones propuestas", ""] + [
        f"{n}. **{o['op']}** {o.get('task_id','')[:12]} {o.get('text','')[:80]} — {o.get('reason','')[:100]}"
        for n, o in enumerate(prop.get("operations") or [], 1)] + [""]
    L += ["## Preguntas", ""] + [f"- {q.get('text','')}" for q in prop.get("questions") or []] + [""]
    tp = prop.get("topics") or []
    L += ["## Temas consolidados", ""] + ([f"- [{t.get('state')}] {t.get('topic')}: {(t.get('summary') or '')[:110]}"
                                           for t in tp] or ["(ninguno)"]) + [""]
    L += ["## Notas", ""] + ([f"- [{n.get('kind','')}] {n.get('date','')} {n.get('text','')[:120]}"
                              f"{' → ' + n['task_id'][:12] if n.get('task_id') else ''}"
                              for n in prop.get("notes") or []] or ["(ninguna)"]) + [""]
    L += ["## Traza", ""] + [f"- {t['node']}: {t['ms']} ms {({k: v for k, v in t.items() if k not in ('node', 'ms')})}"
                              for t in run.get("trace") or []]
    return "\n".join(L)
