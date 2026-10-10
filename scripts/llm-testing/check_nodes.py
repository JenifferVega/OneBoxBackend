#!/usr/bin/env python3
"""Is every LLM the agent uses up, and how fast does it answer?

    python scripts/llm-testing/check_nodes.py                    # every node, as configured in .env
    python scripts/llm-testing/check_nodes.py --node planner     # one node
    python scripts/llm-testing/check_nodes.py --model gemini:gemini-2.5-flash
                                                  # try a candidate on every node
    python scripts/llm-testing/check_nodes.py --repeat 3         # latency over 3 calls
    python scripts/llm-testing/check_nodes.py --planning         # also the replanning LLM

For each node it makes two tiny calls, the two shapes the graph really uses:
  text        "reply with the single word OK"           -> .invoke()
  structured  "2 + 3?" into a Pydantic model {answer:int} -> .with_structured_output()
and prints which provider/model answered, how long it took, and why it failed.

Fallbacks are OFF here on purpose: with them on, a provider that fails every
call still "works" because another one answers, and this script would report
a healthy node that is not. Pass --with-fallbacks to see production behaviour.

Uses the same .env as the backend (main.py loads it). Costs a few hundred
tokens per node per call. Exit code 1 if any check fails.
"""
import argparse
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def _load_env(with_fallbacks: bool) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        print("python-dotenv is not installed: using this shell's environment only")
    if not with_fallbacks:
        os.environ["LLM_FALLBACKS"] = "off"


def _schema():
    from pydantic import BaseModel, Field

    class Sum(BaseModel):
        answer: int = Field(description="The result of the sum, as a number")
        explanation: str = Field(description="One short sentence")
    return Sum


def _text_check(llm) -> tuple:
    from langchain_core.messages import HumanMessage, SystemMessage
    out = llm.invoke([SystemMessage(content="You are a health check. Follow the instruction exactly."),
                      HumanMessage(content="Reply with the single word OK and nothing else.")])
    text = str(getattr(out, "content", out)).strip()
    return "ok" in text.lower()[:20], text[:60]


def _structured_check(llm) -> tuple:
    from langchain_core.messages import HumanMessage, SystemMessage
    out = llm.with_structured_output(_schema()).invoke(
        [SystemMessage(content="You are a health check. Answer in the requested structure."),
         HumanMessage(content="What is 2 + 3?")])
    return getattr(out, "answer", None) == 5, f"answer={getattr(out, 'answer', None)!r}"


def _timed(fn, llm) -> tuple:
    t0 = time.perf_counter()
    try:
        ok, detail = fn(llm)
        err = "" if ok else f"wrong answer: {detail}"
    except Exception as e:
        ok, detail, err = False, "", f"{type(e).__name__}: {str(e)[:220]}"
    return ok, int((time.perf_counter() - t0) * 1000), detail, err


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", default="", help="only this node (context_resolver, planner, validator, narrator)")
    ap.add_argument("--model", default="", help="provider:model to use on the checked node(s) instead of .env")
    ap.add_argument("--repeat", type=int, default=1, help="calls per check, to see latency spread")
    ap.add_argument("--planning", action="store_true", help="also check the replanning LLM (PLANNING_LLM)")
    ap.add_argument("--with-fallbacks", action="store_true", help="production behaviour: fallbacks on")
    args = ap.parse_args()

    _load_env(args.with_fallbacks)
    from agent.graph.llm_factory import NODE_NAMES, create_llm

    nodes = [args.node] if args.node else list(NODE_NAMES)
    unknown = [n for n in nodes if n not in NODE_NAMES]
    if unknown:
        ap.error(f"unknown node {unknown}; use one of {NODE_NAMES}")

    targets = []        # (label, factory)
    for node in nodes:
        targets.append((node, lambda n=node: create_llm(n, env_value=args.model)))
    if args.planning:
        def _planning():
            if args.model:
                return create_llm("planner", env_value=args.model, temperature=0.1, max_tokens=16000,
                                  timeout=300, with_fallbacks=args.with_fallbacks)
            from agent.planning.runner import default_llm
            return default_llm()
        targets.append(("planning (replan)", _planning))

    print(f"\nfallbacks: {'ON (production)' if args.with_fallbacks else 'OFF (each provider measured alone)'}"
          + (f" | model override: {args.model}" if args.model else " | models from .env"))
    print(f"{'node':20} {'provider':13} {'model':34} {'check':11} {'ok':3} {'ms':>7}  detail")
    print("-" * 110)

    failed = 0
    for label, make in targets:
        try:
            llm = make()
            provider, model = llm._names[0] if hasattr(llm, "_names") else ("?", "?")
        except Exception as e:
            failed += 1
            print(f"{label:20} {'-':13} {'-':34} {'config':11} {'NO':3} {'':>7}  {type(e).__name__}: {str(e)[:200]}")
            continue
        for check, fn in (("text", _text_check), ("structured", _structured_check)):
            times, last_err, last_detail, oks = [], "", "", 0
            for _ in range(max(1, args.repeat)):
                ok, ms, detail, err = _timed(fn, llm)
                times.append(ms)
                oks += ok
                last_detail, last_err = detail, (err or last_err)
            all_ok = oks == len(times)
            failed += not all_ok
            ms_txt = (f"{min(times)}-{max(times)}" if len(times) > 1 else str(times[0]))
            note = last_err if not all_ok else last_detail
            if len(times) > 1:
                note = f"{oks}/{len(times)} ok · {note}"
            print(f"{label:20} {provider:13} {model[:34]:34} {check:11} {'yes' if all_ok else 'NO':3} {ms_txt:>7}  {note}")

    print("-" * 110)
    print("ALL UP" if not failed else f"{failed} check(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
