#!/usr/bin/env python3
"""Give the insight cards already stored both languages (es + en).

    python scripts/dynamo/backfill_translations.py                     # DRY RUN: counts + cost
    python scripts/dynamo/backfill_translations.py --sample 10         # translate 10, print, write nothing
    python scripts/dynamo/backfill_translations.py --apply --project <projectId>
    python scripts/dynamo/backfill_translations.py --apply             # everything

For every item of onebox-insights, the fields `title` and `description` are
read, their language detected (es or en) and the OTHER language written by the
model (api/i18n.py: one call per batch, TRANSLATE_LLM, default Gemini 3.5
Flash-Lite). The result goes next to the original, which is never changed:

    i18n = {"title": {"lang": "es", "es": "...", "en": "...", "srcHash": "..."},
            "description": {...}}

Resumable: a field that already has both versions of the SAME original is
skipped, so stopping and running again never pays twice. Every batch is
written as soon as it is translated.

Runs against the tables of the AWS account and region in your environment
(.env of the repo, like the backend). Without --apply nothing is written.
"""
import argparse
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

TABLE = "onebox-insights"
KEY = ("userId", "insightId")
FIELDS = ("title", "description")
BATCH_TEXTS = 40               # texts per model call (also bounded by api/i18n.BATCH_CHARS)
COST_PER_M_CHARS = 0.70        # Gemini 3.5 Flash-Lite ($0.30 in / $2.50 out per M tokens), ~4 chars per token
_LETTERS = re.compile(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{2,}")


def _todo(items, project: str):
    """[(item, field, text)] still missing a version."""
    from api import i18n
    out = []
    for it in items:
        if project and it.get("projectId") != project:
            continue
        for f in FIELDS:
            text = (it.get(f) or "").strip() if isinstance(it.get(f), str) else ""
            if not text or not _LETTERS.search(text):
                continue                       # empty, or only numbers / ids / dates
            if i18n.is_complete((it.get("i18n") or {}).get(f), text):
                continue
            out.append((it, f, text))
    return out


def _write(table, item: dict, field: str, entry: dict) -> None:
    """Merge one field's entry into the item's i18n map (the original is untouched)."""
    cur = dict(item.get("i18n") or {})
    cur[field] = entry
    item["i18n"] = cur                          # the other field of the same item sees it
    table.update_item(Key={k: item[k] for k in KEY}, UpdateExpression="SET i18n = :m",
                      ExpressionAttributeValues={":m": cur})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="translate and write (default: dry run)")
    ap.add_argument("--project", default="", help="only this projectId")
    ap.add_argument("--limit", type=int, default=0, help="at most this many texts")
    ap.add_argument("--sample", type=int, default=0, help="translate N texts and print them; write nothing")
    args = ap.parse_args()

    # The SAME credentials and keys the backend uses (AWS + GEMINI_API_KEY).
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        print("python-dotenv is not installed: using the credentials of this shell")

    from agent.tools.db import insights_table
    from api import i18n
    from api.deps import scan_all_pages

    mode = "APPLY: translations WILL be written" if args.apply else (
        f"SAMPLE: {args.sample} texts translated and printed, nothing written" if args.sample
        else "DRY RUN: nothing will be translated or written (add --apply, or --sample N to see quality)")
    print(mode)
    print(f"model: {i18n.TRANSLATE_LLM} | table: {TABLE}" + (f" | project: {args.project}" if args.project else " | ALL projects"))
    try:
        import boto3
        print(f"AWS identity: {boto3.client('sts').get_caller_identity()['Arn']}")
    except Exception:
        print("AWS identity: (could not be read)")

    items = scan_all_pages(insights_table)
    todo = _todo(items, args.project)
    if args.limit:
        todo = todo[:args.limit]
    chars = sum(len(t) for _i, _f, t in todo)
    done_already = sum(1 for it in items for f in FIELDS
                       if isinstance(it.get(f), str) and i18n.is_complete((it.get("i18n") or {}).get(f), it[f].strip()))
    print(f"\n{len(items)} insight(s) in the table; {done_already} field(s) already have both languages")
    print(f"to translate: {len(todo)} text(s) in {len({id(i) for i, _f, _t in todo})} insight(s), "
          f"{chars:,} characters"
          + (f" -> about ${chars / 1e6 * COST_PER_M_CHARS:.4f} (Gemini 3.5 Flash-Lite price)"
             if i18n.TRANSLATE_LLM.startswith("gemini") else " (cost estimate only for Gemini)"))
    for it, f, t in todo[:5]:
        print(f"  · [{it.get('type', '')}/{f}] {t[:90]!r}")

    if args.sample:
        batch = {str(n): t for n, (_i, _f, t) in enumerate(todo[:args.sample])}
        res = i18n.translate(batch)
        print("")
        for k, t in batch.items():
            r = res.get(k)
            print(f"[{r['lang'] if r else '??'}] {t[:200]}")
            print(f"  -> {r['translation'][:200] if r else '(no answer)'}\n")
        return 0
    if not args.apply:
        print(f"\nRun with --sample 10 to check the quality, then --apply (start with --project).")
        return 0

    t0, written, failed, missing = time.time(), 0, 0, 0
    llm = i18n._llm()
    for start in range(0, len(todo), BATCH_TEXTS):
        chunk = todo[start:start + BATCH_TEXTS]
        res = i18n.translate({str(n): t for n, (_i, _f, t) in enumerate(chunk)}, llm=llm)
        for n, (it, f, text) in enumerate(chunk):
            r = res.get(str(n))
            if not r:
                missing += 1                    # left for the next run
                continue
            try:
                _write(insights_table, it, f, i18n.g_entry(text, r["lang"], r["translation"]))
                written += 1
            except Exception as e:
                failed += 1
                print(f"  FAILED {it.get('insightId')}/{f}: {type(e).__name__}: {e}")
        print(f"  {min(start + BATCH_TEXTS, len(todo))}/{len(todo)} texts  "
              f"({written} written, {missing} without answer, {failed} failed, {time.time() - t0:.0f} s)", flush=True)
    print(f"\nDone: {written} written" + (f", {missing} without answer (run again to retry)" if missing else "")
          + (f", {failed} FAILED" if failed else "") + ".")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
