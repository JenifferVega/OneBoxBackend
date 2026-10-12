#!/usr/bin/env python3
"""Set the "proposal waiting" marker (pendingProposal) on every project.

    python scripts/dynamo/refresh_pending_markers.py            # DRY RUN: lists what it would set
    python scripts/dynamo/refresh_pending_markers.py --apply

New proposals keep the marker up to date by themselves
(planning_chat.refresh_pending_marker on save / decide / stale). This one-time
run covers the proposals that were already waiting before the marker existed.
Runs against the tables of the AWS account in the repo's .env.
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the markers (default: dry run)")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        print("python-dotenv is not installed: using the AWS credentials of this shell")
    from agent.tools.db import projects_table
    from api.deps import scan_all_pages
    from api.services import planning_chat

    print("APPLY: markers WILL be written" if args.apply else "DRY RUN: nothing will be written (add --apply)")
    waiting = 0
    for proj in scan_all_pages(projects_table):
        pid = proj.get("projectId")
        if not pid:
            continue
        item = planning_chat.pending_proposal(pid)
        if item:
            waiting += 1
            print(f"  • {proj.get('name', pid)[:50]}: proposal {item.get('proposalId')} ({(item.get('createdAt') or '')[:10]})")
        if args.apply and (item or proj.get("pendingProposal")):
            planning_chat.refresh_pending_marker(pid)
    print(f"\n{waiting} project(s) with a proposal waiting." + ("" if args.apply else " Run with --apply to mark them."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
