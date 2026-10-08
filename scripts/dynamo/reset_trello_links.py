#!/usr/bin/env python3
"""Forget every link between OneBox and Trello, to start the integration clean.

    python scripts/dynamo/reset_trello_links.py                 # DRY RUN: only reports
    python scripts/dynamo/reset_trello_links.py --apply         # removes the links
    python scripts/dynamo/reset_trello_links.py --apply --project <projectId>
    python scripts/dynamo/reset_trello_links.py --apply --tokens   # also disconnect users

What it removes (only these attributes; every other field is left alone):
  onebox-projects     trelloBoardId, trelloBoardName, trelloListId,
                      trelloListName, trelloInvites, trelloInvitesAt
  onebox-tasks        trelloCardId, trelloCardUrl, trelloSyncHash, trelloCardListId
  onebox-user-tokens  (only with --tokens) trelloToken, trelloUsername,
                      trelloConnected, trelloConnectedAt

What it does NOT touch:
  * Trello itself. Boards, lists, cards and invitations stay in Trello; delete
    them there if you want them gone. After a reset, an export creates NEW
    cards: the old ones are no longer linked to anything.
  * Gmail tokens, which live in the same user-tokens row: attributes are
    REMOVEd one by one, the row is never deleted.

Runs against the tables of the AWS account and region in your environment.
Without --apply nothing is written.
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

PROJECT_ATTRS = ["trelloBoardId", "trelloBoardName", "trelloListId", "trelloListName",
                 "trelloInvites", "trelloInvitesAt"]
TASK_ATTRS = ["trelloCardId", "trelloCardUrl", "trelloSyncHash", "trelloCardListId"]
TOKEN_ATTRS = ["trelloToken", "trelloUsername", "trelloConnected", "trelloConnectedAt"]


def _has_any(item: dict, attrs) -> list:
    return [a for a in attrs if a in item]


def _remove(table, key: dict, attrs: list) -> None:
    names = {f"#a{i}": a for i, a in enumerate(attrs)}
    table.update_item(Key=key, UpdateExpression="REMOVE " + ", ".join(names),
                      ExpressionAttributeNames=names)


def _sweep(label, table, key_names, attrs, apply, only_project=""):
    from api.deps import scan_all_pages
    items = scan_all_pages(table)
    hits = [(it, _has_any(it, attrs)) for it in items]
    hits = [(it, found) for it, found in hits if found
            and (not only_project or it.get("projectId") == only_project)]
    print(f"\n{label}: {len(hits)} of {len(items)} item(s) linked to Trello")
    done = failed = 0
    for it, found in hits:
        key = {k: it[k] for k in key_names}
        desc = it.get("name") or it.get("text") or it.get("trelloUsername") or ""
        print(f"  {'-' if apply else '·'} {key}  {desc[:50]!r}  [{', '.join(found)}]")
        if apply:
            try:
                _remove(table, key, found)
                done += 1
            except Exception as e:
                failed += 1
                print(f"      FAILED: {type(e).__name__}: {e}")
    if apply:
        print(f"  -> cleared {done}" + (f", {failed} FAILED" if failed else ""))
    return len(hits), failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--project", default="", help="only this projectId (and its tasks)")
    ap.add_argument("--tokens", action="store_true",
                    help="also disconnect every user's Trello account (not with --project)")
    args = ap.parse_args()
    if args.tokens and args.project:
        ap.error("--tokens disconnects USERS, not a project: do not combine it with --project")

    # The SAME credentials the backend uses: main.py loads .env, and without
    # this the script fell back to whatever ~/.aws had (a read-only S3 user).
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        print("python-dotenv is not installed: using the AWS credentials of this shell")

    from agent.tools.db import projects_table, tasks_table
    from api.deps import user_tokens_table

    print("APPLY: links WILL be removed" if args.apply else "DRY RUN: nothing will be written (add --apply)")
    print(f"region: {os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1 (default)'}"
          + (f" | project: {args.project}" if args.project else " | ALL projects"))
    try:
        import boto3
        print(f"AWS identity: {boto3.client('sts').get_caller_identity()['Arn']}")
    except Exception:
        print("AWS identity: (could not be read)")

    failed = 0
    total = 0
    n, f = _sweep("onebox-projects", projects_table, ["projectId"], PROJECT_ATTRS, args.apply, args.project)
    total, failed = total + n, failed + f
    n, f = _sweep("onebox-tasks", tasks_table, ["projectId", "taskId"], TASK_ATTRS, args.apply, args.project)
    total, failed = total + n, failed + f
    if args.tokens:
        n, f = _sweep("onebox-user-tokens", user_tokens_table, ["userId"], TOKEN_ATTRS, args.apply)
        total, failed = total + n, failed + f

    if not args.apply:
        print(f"\n{total} item(s) would be changed. Run again with --apply to do it.")
    else:
        print(f"\nDone. Boards and cards still exist in Trello; delete them there if you want them gone.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
