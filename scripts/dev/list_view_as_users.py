#!/usr/bin/env python3
"""Who can I "view as"? Every project owner and participant, with a ready link.

    python scripts/dev/list_view_as_users.py
    python scripts/dev/list_view_as_users.py --project "Marketing"     # projects whose name contains it
    python scripts/dev/list_view_as_users.py --base-url http://localhost:5173

READ ONLY: scans onebox-projects and lists the Cognito users once, writes
nothing. For each person it prints their projects and role (OWNER / member),
whether they have an account, and the link that opens the app as them
(owners get &uid=, which the backend needs to treat them as owner).

Uses the same .env as the backend. DEVELOPMENT ONLY, like the view-as
feature itself (api/view_as.py, frontend src/devViewAs.ts).
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)


def _cognito_users() -> dict:
    """sub -> {email, name}, for every user in the pool (paginated)."""
    import boto3
    pool = os.environ.get("COGNITO_USER_POOL_ID", "us-east-1_b76prubhx")
    cip = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    out, token = {}, None
    while True:
        kw = {"UserPoolId": pool, "Limit": 60}
        if token:
            kw["PaginationToken"] = token
        page = cip.list_users(**kw)
        for u in page.get("Users", []):
            a = {x["Name"]: x["Value"] for x in u.get("Attributes", [])}
            if a.get("sub"):
                out[a["sub"]] = {"email": (a.get("email") or "").lower(), "name": a.get("name", "")}
        token = page.get("PaginationToken")
        if not token:
            return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="", help="only projects whose name contains this (case-insensitive)")
    ap.add_argument("--base-url", default="http://localhost:5173", help="frontend address for the links")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        pass

    from agent.tools.db import projects_table
    from api.deps import scan_all_pages
    from api.services.projects import normalize_participants

    try:
        users = _cognito_users()
    except Exception as e:
        print(f"(Cognito not readable: {type(e).__name__}: {str(e)[:120]} -- owners will show without email)")
        users = {}
    by_email = {u["email"]: sub for sub, u in users.items() if u["email"]}

    people = {}          # email (or owner sub) -> {name, uid, projects: [(name, role)]}
    for p in scan_all_pages(projects_table):
        pname = p.get("name") or p.get("projectId")
        if args.project and args.project.lower() not in pname.lower():
            continue
        owner = p.get("userId", "")
        o = users.get(owner, {})
        okey = o.get("email") or f"(owner without email: {owner})"
        e = people.setdefault(okey, {"name": o.get("name", ""), "uid": owner, "projects": []})
        e["uid"] = owner
        e["projects"].append((pname, "OWNER"))
        for part in normalize_participants(p.get("participants")):
            email = (part.get("email") or "").lower()
            if not email or email == o.get("email"):
                continue
            e = people.setdefault(email, {"name": part.get("name", ""), "uid": by_email.get(email, ""),
                                          "projects": []})
            e["name"] = e["name"] or part.get("name", "")
            e["projects"].append((pname, "member"))

    if not people:
        print("No projects found" + (f" matching '{args.project}'" if args.project else "") + ".")
        return 0

    def sort_key(item):
        email, e = item
        return (not any(r == "OWNER" for _, r in e["projects"]), email)

    print(f"\n{len(people)} people. Open a link to view the app as them "
          f"(the first time, add &key=<VIEW_AS_TOKEN>).\n")
    for email, e in sorted(people.items(), key=sort_key):
        owns = any(r == "OWNER" for _, r in e["projects"])
        account = "account" if e["uid"] else "no account"
        print(f"{email}  {('· ' + e['name']) if e['name'] else ''}  [{account}]")
        for pname, role in sorted(e["projects"], key=lambda x: (x[1] != "OWNER", x[0])):
            print(f"    {role:6}  {pname}")
        if email.startswith("("):
            print("    (cannot view as: no email for this owner)\n")
            continue
        link = f"{args.base_url}/?as={email}" + (f"&uid={e['uid']}" if owns and e["uid"] else "")
        print(f"    {link}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
