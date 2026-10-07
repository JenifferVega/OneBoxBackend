#!/usr/bin/env python3
"""Run planning-graph cases: before.json + input.json -> proposal -> after.json,
scored against expected.json. Real LLM (costs a few cents per case).

    python tests/run_planning_case.py farmacia_reanalyze
    python tests/run_planning_case.py --all
    PLANNING_LLM=anthropic python tests/run_planning_case.py bion_reanalyze

Writes tests/planning/cases/<case>/runs/<timestamp>/ and runs/latest/:
    proposal.json  after.json  report.md  run.json
Exit code 1 if any case fails its thresholds.
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent.planning.runner import run_planning  # noqa: E402
from agent.planning.schedule import apply_proposal  # noqa: E402
from tests.planning.evaluate import evaluate, to_markdown  # noqa: E402
from tests.planning.fixtures import load_case_export  # noqa: E402

CASES = ROOT / "tests" / "planning" / "cases"


def run_case(name: str) -> dict:
    d = CASES / name
    before = load_case_export(d)
    inp = json.loads((d / "input.json").read_text(encoding="utf-8"))
    expected = json.loads((d / "expected.json").read_text(encoding="utf-8")) if (d / "expected.json").exists() else {}
    started = datetime.now()
    run = run_planning(before, inp.get("trigger", "reanalyze"), today=inp.get("today", ""),
                       message=inp.get("message", ""), document=inp.get("document", ""),
                       mode=inp.get("mode", ""))
    run["seconds"] = round((datetime.now() - started).total_seconds(), 1)
    after = apply_proposal(before, run["proposal"], None, actor="test")[0] if run.get("proposal") else before
    result = evaluate(run.get("proposal"), before, expected) if expected else {"passed": True, "rows": [], "plan_items": [],
                                                                              "violations": [], "existing_accuracy": 0, "plan_items_recall": 0,
                                                                              "counts": {"operations": 0, "creates": 0, "questions": 0}}
    out = d / "runs" / started.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "proposal.json").write_text(json.dumps(run.get("proposal"), ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "after.json").write_text(json.dumps(after, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "run.json").write_text(json.dumps({k: v for k, v in run.items() if k != "result_export"},
                                             ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(to_markdown(name, result, run), encoding="utf-8")
    (out / "result.json").write_text(json.dumps({k: v for k, v in result.items() if k != "rows"},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    latest = d / "runs" / "latest"
    if latest.exists():
        shutil.rmtree(latest)
    shutil.copytree(out, latest)
    print(f"{'PASS' if result['passed'] else 'FAIL'}  {name}  existing={result['existing_accuracy']:.0%} "
          f"plan={result['plan_items_recall']:.0%} ops={result['counts']['operations']} "
          f"q={result['counts']['questions']} {run['seconds']}s -> {latest / 'report.md'}")
    for v in result["violations"]:
        print("      !", v)
    return result


def main(argv):
    names = sorted(p.name for p in CASES.iterdir() if (p / "before.json").exists()) if "--all" in argv \
        else [a for a in argv if not a.startswith("-")]
    if not names:
        print(__doc__)
        return 2
    results = [run_case(n) for n in names]
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
