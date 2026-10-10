#!/usr/bin/env python3
"""Measure models on the agent's own conversations: intelligence, speed, cost.

    python tests/bench/run_bench.py --models gemini:gemini-2.5-flash,ollama-cloud:gpt-oss:120b \
                                    --nodes narrator --cases quick

For each model it routes the chosen node(s) to it (the other nodes keep their
.env model), runs the conversation cases in tests/cases in DEBUG mode
(simulated tools: nothing touches DynamoDB, Trello or WhatsApp; the LLMs are
real), grades every turn with the same checks as tests/run_case.py, and
records every LLM call: which node, which model, ok or failed, how long, and
the tokens when the provider reports them.

  --models   comma-separated list, each one measured separately:
               a configuration NAME from tests/bench/configs.json (A, B, O1...),
               which sets all four nodes; or a provider:model applied to
               --nodes; or "env" = the .env routing, as a baseline.
  --nodes    all | planner | narrator | validator | context_resolver | a,b
  --cases    quick | trello | member | all | name1,name2   (default quick)
  --repeat   run every case N times (LLMs are not deterministic)

Fallbacks are OFF: a model that fails must show up as a failure, not be
silently replaced by another provider.

Report: tests/bench/runs/<timestamp>/report.md (+ results.json), copied to
tests/bench/runs/latest/.
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CASES_DIR = ROOT / "tests" / "cases"
RUNS = ROOT / "tests" / "bench" / "runs"
NODES = ("context_resolver", "planner", "validator", "narrator")

# A spread of what the agent must get right: refusing to guess, multi-step
# Trello, consent, permissions, reading results it was given.
QUICK = [
    "ambiguous_project_must_ask",
    "update_nonexistent_task_must_refuse",
    "notify_person_without_phone",
    "gantt_which_task_is_missing",
    "trello_project_not_found",
    "trello_list_choice_must_offer_both",
    "confirmation_yes_executes",
    "trello_task_edit_syncs_itself",
    "member_status_needs_comment",
    "member_cannot_change_plan",
]


def _select_cases(spec: str) -> list:
    every = sorted(p.stem for p in CASES_DIR.glob("*.json") if not p.stem.startswith("_"))
    if spec == "quick":
        return [c for c in QUICK if c in every]
    if spec == "all":
        return every
    if spec in ("trello", "member"):
        out = []
        for c in every:
            d = json.loads((CASES_DIR / f"{c}.json").read_text(encoding="utf-8"))
            if spec == "member" and (d.get("as") or any(t.get("as") for t in d.get("turns", []))):
                out.append(c)
            if spec == "trello" and "trello" in json.dumps(d).lower():
                out.append(c)
        return out
    names = [c.strip() for c in spec.split(",") if c.strip()]
    missing = [c for c in names if c not in every]
    if missing:
        raise SystemExit(f"unknown case(s): {missing}")
    return names


class Meter:
    """Every LLM event of the run, from agent.obs, tagged with the current case."""

    def __init__(self):
        self.events = []
        self.tags = {}               # thread -> {model_label, case, turn, rep}
        self._last_tokens = {}       # thread -> (prompt, output) of its last response

    def __call__(self, event, fields):
        tid = threading.get_ident()
        if event == "llm_response":
            self._last_tokens[tid] = (int(fields.get("prompt_tokens") or 0),
                                      int(fields.get("output_tokens") or 0))
        elif event == "llm_attempt":
            pt, ot = self._last_tokens.pop(tid, (0, 0)) if fields.get("ok") else (0, 0)
            self.events.append({**self.tags.get(tid, {}), "node": fields.get("node"), "provider": fields.get("provider"),
                                "model": fields.get("model"), "ok": bool(fields.get("ok")),
                                "ms": int(fields.get("ms") or 0), "prompt_tokens": pt, "output_tokens": ot,
                                "error": (fields.get("error") or "")[:200]})


CONFIGS = json.loads((ROOT / "tests" / "bench" / "configs.json").read_text(encoding="utf-8"))


def _route(model: str, nodes: list, base_env: dict) -> None:
    for n in NODES:
        os.environ.pop(f"NODE_LLM_{n.upper()}", None)
        if base_env.get(n):
            os.environ[f"NODE_LLM_{n.upper()}"] = base_env[n]
    if model in CONFIGS:
        for n in NODES:
            os.environ[f"NODE_LLM_{n.upper()}"] = CONFIGS[model][n]
    elif model != "env":
        for n in nodes:
            os.environ[f"NODE_LLM_{n.upper()}"] = model
    # llm_factory reads LLM_PROVIDER_ALL before the per-node table; it would
    # override the routing measured here.
    os.environ.pop("LLM_PROVIDER_ALL", None)


def _turn(spec, case, history, session, meter, tag, timeout_s):
    """One turn in a worker thread, so a model that hangs costs at most
    timeout_s. The thread cannot be killed: it is abandoned and keeps its own
    tag, so whatever it logs later is not counted against another turn."""
    import threading as _t
    from agent.graph import runner
    from agent.tools import clear_current_user, set_current_user
    box = {}

    def work():
        meter.tags[_t.get_ident()] = tag
        who = spec.get("as") or case.get("as") or {}
        set_current_user(who.get("uid") or "bench-owner", (who.get("email") or "bench-owner@onebox.local").lower())
        try:
            box["res"] = runner.run_agent(spec["message"], history, debug_mode=True, session_id=session)
        except Exception as e:
            box["err"] = f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            clear_current_user()
    th = _t.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        return None, f"TIMEOUT: no answer after {timeout_s} s (turn abandoned)"
    return box.get("res"), box.get("err", "")


def _run_case(name: str, meter: Meter, model_label: str, rep: int, timeout_s: int = 120) -> dict:
    from agent.graph import runner
    from agent.graph.nodes.executor.node import clear_dry_run_cache
    from tests.run_case import check_turn

    case = json.loads((CASES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    session = f"bench-{name}-{rep}"
    clear_dry_run_cache(session)
    runner.clear_last_results(session)
    history, turns = [], []
    for spec in case["turns"]:
        tag = {"model_label": model_label, "case": name, "turn": spec.get("n"), "rep": rep}
        t0 = time.perf_counter()
        res, error = _turn(spec, case, history, session, meter, tag, timeout_s)
        secs = round(time.perf_counter() - t0, 1)
        if res is not None and not error:
            reply = {"response": res.get("response", ""), "toolsUsed": res.get("tools_used", []),
                     "debug_info": res.get("debug_info") or {}}
            checks = check_turn(spec, reply)
        else:
            reply, checks = {"response": "", "toolsUsed": []}, []
        # A turn whose model never answered is a FAILED turn, whatever the
        # checks say: "must not do X" checks pass trivially when nothing ran,
        # which made a dead model score 80%.
        mine = [e for e in meter.events if e.get("case") == name and e.get("turn") == spec.get("n")
                and e.get("rep") == rep and e.get("model_label") == model_label]
        if not error and not any(e["ok"] for e in mine):
            error = "no LLM call succeeded in this turn" + (f": {mine[-1]['error']}" if mine else
                                                             " (the graph failed before calling a model)")
        history = history + [{"role": "user", "content": spec["message"]},
                             {"role": "assistant", "content": reply.get("response", "")}]
        passed = 0 if error else sum(1 for s, _ in checks if s == "PASS")
        failed = [label for s, label in checks if s == "FAIL"]
        turns.append({"n": spec.get("n"), "message": spec["message"], "seconds": secs,
                      "tools": reply.get("toolsUsed", []), "response": reply.get("response", "")[:600],
                      "checks": len([1 for s, _ in checks if s != "SKIP"]), "passed": passed,
                      "failed": failed, "error": error,
                      "ok": not error and not failed})
        if error.startswith("TIMEOUT"):
            break                          # the rest of this case depends on this turn
    return {"case": name, "rep": rep, "turns": turns, "ok": all(t["ok"] for t in turns)}


def _summary(label: str, cases: list, events: list) -> dict:
    from tests.bench.prices import cost
    turns = [t for c in cases for t in c["turns"]]
    checks = sum(t["checks"] for t in turns)
    passed = sum(t["passed"] for t in turns)
    by_node = {}
    for e in events:
        if e["model_label"] != label:
            continue
        b = by_node.setdefault(e["node"], {"calls": 0, "failed": 0, "ms": [], "prompt_tokens": 0,
                                           "output_tokens": 0, "cost": 0.0, "cost_known": True,
                                           "models": set(), "errors": []})
        b["calls"] += 1
        b["failed"] += not e["ok"]
        b["ms"].append(e["ms"])
        b["prompt_tokens"] += e["prompt_tokens"]
        b["output_tokens"] += e["output_tokens"]
        b["models"].add(f"{e['provider']}:{e['model']}")
        if not e["ok"] and len(b["errors"]) < 3:
            b["errors"].append(e["error"])
        c = cost(e["model"], e["prompt_tokens"], e["output_tokens"]) if e["ok"] else 0.0
        if e["ok"] and (c is None or (e["prompt_tokens"] == 0 and e["output_tokens"] == 0)):
            b["cost_known"] = False
        b["cost"] += c or 0.0
    nodes = {}
    for n, b in by_node.items():
        ms = sorted(b["ms"])
        nodes[n] = {"models": sorted(b["models"]), "calls": b["calls"], "failed": b["failed"],
                    "avg_ms": int(statistics.mean(ms)) if ms else 0,
                    "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else 0,
                    "prompt_tokens": b["prompt_tokens"], "output_tokens": b["output_tokens"],
                    # no successful call = nothing to price, not "free"
                    "cost_usd": round(b["cost"], 5) if b["cost_known"] and b["calls"] > b["failed"] else None,
                    "errors": b["errors"]}
    return {"model": label, "cases": len(cases), "cases_ok": sum(c["ok"] for c in cases),
            "turns": len(turns), "turns_ok": sum(t["ok"] for t in turns),
            "checks": checks, "checks_passed": passed,
            "score": round(100 * passed / checks, 1) if checks else 0.0,
            "turn_seconds_avg": round(statistics.mean([t["seconds"] for t in turns]), 1) if turns else 0,
            "nodes": nodes}


def _report(meta: dict, summaries: list, results: dict) -> str:
    L = [f"# Model benchmark — {meta['started']}", "",
         f"Nodes measured: **{', '.join(meta['nodes'])}** · cases: {meta['cases_spec']} "
         f"({len(meta['cases'])}) · repeat: {meta['repeat']} · fallbacks OFF", "",
         "| Model | Score (checks) | Turns OK | Cases OK | Avg turn | Measured node(s): avg / p95 ms | LLM failures | Est. cost |",
         "|---|---|---|---|---|---|---|---|"]
    for s in summaries:
        measured = [s["nodes"][n] for n in meta["nodes"] if n in s["nodes"]]
        lat = ", ".join(f"{n} {s['nodes'][n]['avg_ms']}/{s['nodes'][n]['p95_ms']}"
                        for n in meta["nodes"] if n in s["nodes"]) or "-"
        fails = sum(m["failed"] for m in measured)
        calls = sum(m["calls"] for m in measured)
        costs = [m["cost_usd"] for m in measured]
        cost_txt = "n/a" if (not costs or any(c is None for c in costs)) else f"${sum(costs):.4f}"
        name = f"`{s['model']}` {CONFIGS[s['model']]['label']}" if s['model'] in CONFIGS else f"`{s['model']}`"
        if s.get("too_slow"):
            name += " ⚠️ too slow, stopped"
        L.append(f"| {name} | **{s['score']}%** ({s['checks_passed']}/{s['checks']}) | "
                 f"{s['turns_ok']}/{s['turns']} | {s['cases_ok']}/{s['cases']} | {s['turn_seconds_avg']} s | "
                 f"{lat} | {fails}/{calls} | {cost_txt} |")
    base = next((x for x in summaries if x["model"] in ("A", "env")), None)
    if base and len(summaries) > 1:
        def pct(v, b):
            return "—" if not b else f"{(v - b) / b * 100:+.0f}%"
        L += ["", f"### Speed vs baseline `{base['model']}`", "",
              "| Config | Avg turn | vs baseline | planner avg | resolver avg | validator avg | narrator avg |",
              "|---|---|---|---|---|---|---|"]
        for x in summaries:
            cells = []
            for n in ("planner", "context_resolver", "validator", "narrator"):
                v, b = x["nodes"].get(n, {}).get("avg_ms", 0), base["nodes"].get(n, {}).get("avg_ms", 0)
                cells.append(f"{v} ms ({pct(v, b)})" if x is not base else f"{v} ms")
            L.append(f"| `{x['model']}` | {x['turn_seconds_avg']} s | "
                     f"{'—' if x is base else pct(x['turn_seconds_avg'], base['turn_seconds_avg'])} | "
                     + " | ".join(cells) + " |")
    L += ["", "Score = share of the cases' checks that passed (tools used, tool results, what the reply "
          "says or must not say). Cost covers the measured node(s) only and is an estimate from "
          "tests/bench/prices.py; n/a when the provider does not report tokens (Anthropic, Bedrock).", ""]
    for s in summaries:
        L += [f"## `{s['model']}`" + (f" — {CONFIGS[s['model']]['label']}" if s['model'] in CONFIGS else ""), ""]
        for n, d in s["nodes"].items():
            mark = " (measured)" if n in meta["nodes"] else ""
            L.append(f"- **{n}**{mark}: {', '.join(d['models'])} · {d['calls']} calls, {d['failed']} failed · "
                     f"avg {d['avg_ms']} ms, p95 {d['p95_ms']} ms · tokens {d['prompt_tokens']} in / "
                     f"{d['output_tokens']} out")
            for err in d["errors"]:
                L.append(f"  - error: `{err}`")
        L.append("")
        for c in results[s["model"]]:
            L.append(f"### {'✅' if c['ok'] else '❌'} {c['case']}" + (f" (rep {c['rep']})" if meta["repeat"] > 1 else ""))
            for t in c["turns"]:
                L.append(f"- T{t['n']} {'✅' if t['ok'] else '❌'} {t['seconds']} s · tools: {', '.join(t['tools']) or '-'}")
                if t["error"]:
                    L.append(f"  - ERROR: {t['error']}")
                for f in t["failed"]:
                    L.append(f"  - failed: {f}")
                if not t["ok"]:
                    L.append(f"  - reply: {t['response'][:300]!r}")
            L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", required=True)
    ap.add_argument("--nodes", default="all")
    ap.add_argument("--cases", default="quick")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--turn-timeout", type=int, default=120,
                    help="seconds a turn may take; slower counts as failed")
    ap.add_argument("--max-timeouts", type=int, default=2,
                    help="a configuration with this many timed-out turns is stopped (too slow to be usable)")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    os.environ["LLM_FALLBACKS"] = "off"
    base_env = {n: os.environ.get(f"NODE_LLM_{n.upper()}", "") for n in NODES}

    nodes = list(NODES) if args.nodes == "all" else [n.strip() for n in args.nodes.split(",") if n.strip()]
    bad = [n for n in nodes if n not in NODES]
    if bad:
        raise SystemExit(f"unknown node(s) {bad}; use {NODES} or all")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    from agent.graph.llm_factory import _parse_config
    for m in models:
        if m in CONFIGS:
            for n in NODES:
                _parse_config(CONFIGS[m][n])
            nodes = list(NODES) if args.nodes == "all" else nodes
        elif m != "env":
            _parse_config(m)           # fail now, not after an hour
    cases = _select_cases(args.cases)

    from agent import obs
    meter = Meter()
    obs.add_listener(meter)
    from agent.graph import runner

    started = datetime.now()
    meta = {"started": started.strftime("%Y-%m-%d %H:%M"), "models": models, "nodes": nodes,
            "cases_spec": args.cases, "cases": cases, "repeat": args.repeat}
    print(f"[bench] {len(models)} model(s) x {len(cases)} case(s) x {args.repeat}, nodes={nodes}", flush=True)

    out = RUNS / started.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    latest = RUNS / "latest"          # overwritten file by file: no deletes needed
    latest.mkdir(parents=True, exist_ok=True)

    def save(done: bool):
        """Written after EVERY configuration: stopping the run never loses
        what already finished."""
        meta["finished"] = done
        (out / "results.json").write_text(json.dumps({"meta": meta, "summaries": summaries, "results": results,
                                                      "llm_calls": meter.events}, ensure_ascii=False, indent=1,
                                                     default=str), encoding="utf-8")
        (out / "report.md").write_text(_report(meta, summaries, results)
                                       + ("" if done else "\n\n_(partial: the run is still going)_"),
                                       encoding="utf-8")
        for f in ("results.json", "report.md"):
            shutil.copy2(out / f, latest / f)

    results, summaries = {}, []
    for m in models:
        _route(m, nodes, base_env)
        runner.reset_graph()
        results[m] = []
        timeouts = 0
        for rep in range(1, args.repeat + 1):
            for c in cases:
                if timeouts >= args.max_timeouts:
                    print(f"[bench] {m} · {c} · SKIPPED: {timeouts} turns over {args.turn_timeout} s", flush=True)
                    continue
                print(f"[bench] {m} · {c} · rep {rep}", flush=True)
                r = _run_case(c, meter, m, rep, args.turn_timeout)
                timeouts += sum(1 for t in r["turns"] if t["error"].startswith("TIMEOUT"))
                results[m].append(r)
        summaries.append(_summary(m, results[m], meter.events))
        if timeouts >= args.max_timeouts:
            summaries[-1]["too_slow"] = True
        s = summaries[-1]
        print(f"[bench] {m}: score {s['score']}%, turns {s['turns_ok']}/{s['turns']}"
              + (" · TOO SLOW, stopped" if s.get("too_slow") else ""), flush=True)
        save(done=False)

    save(done=True)
    print(f"[bench] report: {out / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
