"""Simulate the update loop on the REAL data, step by step, as a person would live it.

Run from OneBoxBackend with the backend's .env (same AWS account / tables):

    python scripts/sim/update_flow.py ses [--send-test ventas.javc@gmail.com]   # can SES send? what went out?
    python scripts/sim/update_flow.py tasks [--project <id>]   # every participant and their open tasks
    python scripts/sim/update_flow.py status   --email ventas.javc@gmail.com
    python scripts/sim/update_flow.py make-due --email ventas.javc@gmail.com [--project <id>]
    python scripts/sim/update_flow.py eventbridge --email ventas.javc@gmail.com [--api http://localhost:8000]
    #   -> the 'hora de actualizar' email reaches the inbox; log in: the update modal is there
    #   -> answer it in the app, then:
    python scripts/sim/update_flow.py eventbridge --email ventas.javc@gmail.com   # digest + proposal for the owner
    python scripts/sim/update_flow.py notifications --project <id>
    python scripts/sim/update_flow.py owner-late --email ventas.javc@gmail.com --project <id>
    python scripts/sim/update_flow.py eventbridge --email ventas.javc@gmail.com   # the owner is told he did not report

What each step touches:
  make-due / owner-late  write ONLY the check-in clock of that person (item
                         checkin#<person> in onebox-planning). Nothing else.
  eventbridge            POSTs /api/scheduled/dispatch-pending?force=true, exactly what
                         EventBridge calls every hour (x-cron-secret from .env). force skips
                         the hour-of-day gates and includes the project owner, so you get
                         the email even on your own projects. REAL emails go out, unless
                         the backend runs with EMAIL_OUTBOX=<folder> (then they are files).
  status / notifications / ses only read (ses --send-test sends ONE real email).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")        # Windows console
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from agent.planning.sources import normalize_participant  # noqa: E402
from api.services import checkin  # noqa: E402


def _projects_of(email: str, only: str = ""):
    """[(project, person, open tasks assigned)] where this email is a participant."""
    from agent.tools import projects_table
    from api.deps import scan_all_pages
    out = []
    for proj in scan_all_pages(projects_table):
        if only and proj.get("projectId") != only:
            continue
        if proj.get("status") in ("archived", "completed", "closed"):
            continue
        parts = [normalize_participant(p) for p in proj.get("participants") or []]
        me = next((p for p in parts if (p.get("email") or "").lower() == email), None)
        if not me:
            continue
        tasks = next((ts for p, ts in checkin.assignments(proj, checkin._project_tasks(proj["projectId"]))
                      if checkin.person_key(p) == checkin.person_key(me)), [])
        out.append((proj, me, tasks))
    return out


def _owner(proj) -> str:
    return checkin._owner_email(proj.get("userId", "")) or proj.get("userId", "")


def cmd_status(a):
    rows = _projects_of(a.email, a.project)
    if not rows:
        print(f"{a.email} is not a participant (with that email) of any active project.")
        return
    now = datetime.utcnow()
    for proj, me, tasks in rows:
        key = checkin.person_key(me)
        st = checkin._state(proj["projectId"], key)
        resp, waits, items = checkin.responsible_tasks(proj["projectId"], key, me, tasks) if tasks else (tasks, [], [])
        waiting_dates = checkin.committed(items, now.date())
        d = checkin.due_state(resp, st, now, None, waiting_dates) if resp else None
        role = "OWNER" if _owner(proj) == a.email else "member"
        print(f"\n== {proj.get('name')}  ({proj['projectId']})  you are: {role}, as '{me.get('name')}'")
        print(f"   open tasks assigned to you: {len(tasks)}")
        for t in sorted(tasks, key=lambda t: t.get("dueDate") or "9999")[:8]:
            print(f"     - [{t.get('status')}] {t.get('dueDate') or '----------'}  {(t.get('text') or '')[:70]}")
        if not tasks:
            from collections import Counter
            open_ = [t for t in checkin._project_tasks(proj["projectId"]) if t.get("status") in checkin.OPEN]
            who = Counter((t.get("assignedTo") or "(sin asignar)").strip() for t in open_)
            print(f"   open tasks in the project: {len(open_)}; assigned to: "
                  + (", ".join(f"{k} ({n})" for k, n in who.most_common(8)) or "-"))
            print(f"   -> nothing assigned to '{me.get('name')}' / {a.email}: no modal and no email here. Assign "
                  f"yourself an open task in the app (or rename the participant to match how tasks name you).")
            continue
        print(f"   last report: {st.get('lastReportAt') or 'never'}   reminder email sent: {st.get('reminderSentAt') or 'never'}"
              f"   owner told: {st.get('ownerNotifiedAt') or 'never'}")
        labels = {t.get("taskId"): (t.get("text") or "")[:60] for t in tasks}
        print(f"   last update recorded {len(items)} item(s):")
        for i in items:
            print(f"     · {i.get('type', '?'):12} task={labels.get(i.get('task_id'), i.get('task_id') or '(none)')!r}"
                  f" date={i.get('due_date') or '-'} owner={i.get('owner') or '-'}  \"{(i.get('text') or '')[:70]}\"")
        print("   already answered with a date not reached yet (not asked again): "
              + (", ".join(labels.get(t, t) for t in waiting_dates) or "none"))
        print("   waiting on someone else (not asked; the lead is told): "
              + (", ".join(f"{labels.get(w['task_id'], w['task_id'])} -> "
                           f"{'owner approval (reported done)' if w.get('kind') == 'approval' else w['owner']}"
                           for w in waits) or "none"))
        if not d:
            print("   update due now: no (every open task waits on someone else)")
        else:
            print(f"   update due now: {'YES' if d['due'] else 'no'} (due at {d['due_at']:%Y-%m-%d %H:%M} UTC"
                  f"{', urgent task' if d['urgent'] else ''})")
        if st.get("prep"):
            prep = json.loads(st["prep"])
            print("   prepared questions: " + " | ".join(prep.get("questions") or []))


def _reset_clock(a, days: int, label: str):
    rows = [r for r in _projects_of(a.email, a.project) if r[2]]
    if not rows:
        print("Nothing to do: no project where you have open tasks assigned (see 'status').")
        return
    stamp = (datetime.utcnow() - timedelta(days=days)).isoformat()
    for proj, me, tasks in rows:
        key = checkin.person_key(me)
        item = {**checkin._state(proj["projectId"], key)}
        for k in ("reminderSentAt", "ownerNotifiedAt", "prep"):
            item.pop(k, None)
        item.update(projectId=proj["projectId"], itemId=f"checkin#{key}", lastReportAt=stamp,
                    firstSeenAt=min(item.get("firstSeenAt") or stamp, stamp), lastReportBy=f"sim:{label}")
        from api.services import planning_chat
        planning_chat.table().put_item(Item=item)
        print(f"{proj.get('name')}: last report set to {stamp[:16]} ({days} days ago) for {me.get('name')}.")
    print(f"Next: python scripts/sim/update_flow.py eventbridge --email {a.email}")


def cmd_make_due(a):
    _reset_clock(a, checkin.CHECKIN_DAYS + 1, "make-due")
    print("Log in to the app: the update modal should be waiting (it reads the same clock).")


def cmd_owner_late(a):
    _reset_clock(a, checkin.CHECKIN_DAYS + checkin.CHECKIN_GRACE_DAYS + 1, "owner-late")
    print("The next eventbridge call tells the project owner this person has not reported.")


def cmd_eventbridge(a):
    import requests
    url = f"{a.api.rstrip('/')}/api/scheduled/dispatch-pending"
    headers = {"x-cron-secret": os.environ.get("CRON_SECRET", "")}
    if not a.email and not a.everyone:
        print("Say who: --email <person> (only that person gets anything), or --everyone (REAL emails to every\n"
              "participant whose update is due, in every project -- exactly what the hourly cron does).")
        sys.exit(1)
    params = {"force": "true" if a.force else "false"}
    if a.email:
        params["only"] = a.email
    print(f"POST {url}?{'&'.join(f'{k}={v}' for k, v in params.items())}   (what EventBridge calls every hour)")
    try:
        r = requests.post(url, params=params, headers=headers, timeout=600)
    except requests.exceptions.ConnectionError:
        print(f"\nNo backend answering at {a.api}.\n"
              "  - Start it (from OneBoxBackend):  python main.py --port 8000\n"
              "  - If it runs on WINDOWS and this script in WSL, 'localhost' is not the same machine:\n"
              "      python scripts/sim/update_flow.py eventbridge --api http://$(ip route | awk '/default/ {print $3}'):8000\n"
              "    or run the backend and this script in the same place (both in WSL or both in Windows).")
        sys.exit(1)
    print(f"HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        print(r.text[:2000])
        return
    if r.status_code != 200:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        return
    rem = (data.get("update_reminders") or {}).get("sent") or []
    print(f"\n'Hora de actualizar' emails: {len(rem)}")
    for s in rem:
        print(f"  - {s.get('person')} / {s.get('projectId')}: {s.get('status')}")
        for q in s.get("questions") or []:
            print(f"      ? {q}")
    late = (data.get("checkin_overdue") or {}).get("projects") or []
    print(f"\nOwner told about people who did not report: {len(late)}")
    for p in late:
        print(f"  - {p.get('projectId')}: {', '.join(p.get('people') or [])}")
    dig = (data.get("updates_digest") or {}).get("projects") or []
    print(f"\nDaily digests (members' updates -> summary + proposal for the owner): {len(dig)}")
    for p in dig:
        print(f"  - {p.get('projectId')}: {p.get('status')} proposal={p.get('proposalId')}")
    rest = {k: v for k, v in data.items() if k not in ("update_reminders", "checkin_overdue", "updates_digest")}
    if rest:
        print("\nOther scheduled notifications: " + json.dumps(rest, ensure_ascii=False, default=str)[:600])
    if a.raw:
        print("\n" + json.dumps(data, ensure_ascii=False, indent=2, default=str))


def cmd_notifications(a):
    """The owner's last in-app notifications for a project (the bell)."""
    from boto3.dynamodb.conditions import Key
    from agent.tools import notifications_table, projects_table
    proj = projects_table.get_item(Key={"projectId": a.project}).get("Item")
    if not proj:
        print("Project not found")
        return
    items = notifications_table.query(KeyConditionExpression=Key("userId").eq(proj["userId"]),
                                      ScanIndexForward=False, Limit=50).get("Items", [])
    items = [i for i in items if i.get("projectId") == a.project][: a.limit]
    print(f"Last notifications of the owner of {proj.get('name')} ({_owner(proj)}):")
    for i in items:
        print(f"\n[{(i.get('createdAt') or '')[:16]}] {i.get('type')} | {i.get('title')}")
        print("   " + (i.get("message") or "").replace("\n", "\n   ")[:800])


def cmd_tasks(a):
    """Every participant of a project (or of all projects) with the open tasks
    the update loop assigns to them, plus tasks whose assignee matches nobody."""
    from agent.tools import projects_table
    from api.deps import scan_all_pages
    projs = [projects_table.get_item(Key={"projectId": a.project}).get("Item")] if a.project \
        else list(scan_all_pages(projects_table))
    for proj in [p for p in projs if p]:
        if not a.project and proj.get("status") in ("archived", "completed", "closed"):
            continue
        tasks = checkin._project_tasks(proj["projectId"])
        open_ = [t for t in tasks if t.get("status") in checkin.OPEN]
        print(f"\n== {proj.get('name')}  ({proj['projectId']})  owner: {_owner(proj)}  "
              f"tasks: {len(tasks)} ({len(open_)} open)")
        matched = {}
        for person, ts in checkin.assignments(proj, tasks):
            matched[checkin.person_key(person)] = ts
        for p in [normalize_participant(x) for x in proj.get("participants") or []]:
            ts = matched.get(checkin.person_key(p), [])
            print(f"   - {p.get('name') or '?':28} {p.get('email') or '(no email)':34} {len(ts)} open tasks")
            for t in sorted(ts, key=lambda t: t.get("dueDate") or "9999")[: a.limit]:
                print(f"        [{t.get('status')}] {t.get('dueDate') or '----------'}  {(t.get('text') or '')[:70]}")
        ids = {t["taskId"] for ts in matched.values() for t in ts}
        orphan = [t for t in open_ if t["taskId"] not in ids]
        if orphan:
            print(f"   open tasks that match NO participant ({len(orphan)}): nobody is asked about these")
            for t in orphan[: a.limit]:
                print(f"        [{t.get('status')}] assignedTo='{t.get('assignedTo') or ''}'  {(t.get('text') or '')[:60]}")


def cmd_ses(a):
    """Is SES able to send, from which address, and what happened lately?
    Reads the SES account (quota, 14-day stats) and, with --send-test, sends
    one real test email through the same function the app uses."""
    from agent.tools.channels import SES_FROM_EMAIL, SES_REGION, SES_SANDBOX, _get_ses_client
    ses = _get_ses_client()
    print(f"Sender (SES_FROM_EMAIL): {SES_FROM_EMAIL}   region: {SES_REGION}   SES_SANDBOX={SES_SANDBOX}")
    if os.environ.get("EMAIL_OUTBOX"):
        print(f"EMAIL_OUTBOX={os.environ['EMAIL_OUTBOX']}: emails are written to files, NOT sent.")
        return
    try:
        enabled = ses.get_account_sending_enabled().get("Enabled")
        q = ses.get_send_quota()
        print(f"Sending enabled: {enabled}   sent last 24 h: {int(q.get('SentLast24Hours', 0))} "
              f"of {int(q.get('Max24HourSend', 0))}   max rate: {q.get('MaxSendRate')}/s")
        if int(q.get("Max24HourSend", 0)) == 200:
            print("  (200/day is the SANDBOX quota: only verified addresses receive)")
    except Exception as e:
        print(f"Could not read the SES account: {e}")
        return
    domain = SES_FROM_EMAIL.split("@")[-1]
    try:
        attrs = ses.get_identity_verification_attributes(Identities=[SES_FROM_EMAIL, domain])["VerificationAttributes"]
        for ident in (SES_FROM_EMAIL, domain):
            print(f"Identity {ident}: {attrs.get(ident, {}).get('VerificationStatus', 'not registered')}")
        dk = ses.get_identity_dkim_attributes(Identities=[domain])["DkimAttributes"].get(domain, {})
        print(f"DKIM for {domain}: {dk.get('DkimVerificationStatus', 'not set')} (enabled={dk.get('DkimEnabled')})")
    except Exception as e:
        print(f"Could not read identities: {e}")
    try:
        points = sorted(ses.get_send_statistics().get("SendDataPoints", []), key=lambda p: p["Timestamp"])
        tot = {k: sum(int(p.get(k, 0)) for p in points) for k in ("DeliveryAttempts", "Bounces", "Complaints", "Rejects")}
        print(f"Last 14 days: {tot['DeliveryAttempts']} sent, {tot['Bounces']} bounced, "
              f"{tot['Complaints']} marked as spam, {tot['Rejects']} rejected")
        last = [p for p in points if int(p.get("DeliveryAttempts", 0))][-5:]
        for p in last:
            print(f"   {p['Timestamp']:%Y-%m-%d %H:%M} UTC  sent={p['DeliveryAttempts']} bounces={p['Bounces']}")
        if not tot["DeliveryAttempts"]:
            print("   -> nothing went out in 14 days: the hourly cron (EventBridge) is probably not running.")
    except Exception as e:
        print(f"Could not read statistics: {e}")
    if a.send_test:
        from agent.tools.emails import send_email
        r = send_email(a.send_test, "OneBox: prueba de envío",
                       "Este es un correo de prueba de OneBox (scripts/sim/update_flow.py ses).\n"
                       "Si lo recibes, SES y el remitente funcionan.")
        print(f"Test email to {a.send_test}: {r.get('status') or r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "make-due", "owner-late"):
        p = sub.add_parser(name)
        p.add_argument("--email", required=True)
        p.add_argument("--project", default="", help="only this projectId")
    p = sub.add_parser("eventbridge")
    p.add_argument("--api", default=os.environ.get("ONEBOX_API", "http://localhost:8000"))
    p.add_argument("--no-force", dest="force", action="store_false", help="behave exactly like the hourly cron")
    p.add_argument("--email", default="", help="ONLY this person (recommended for tests)")
    p.add_argument("--everyone", action="store_true", help="everybody, like the real cron (real emails!)")
    p.add_argument("--raw", action="store_true", help="also print the full JSON answer")
    p = sub.add_parser("tasks", help="participants of a project (or all) with their open tasks")
    p.add_argument("--project", default="", help="projectId; empty = every active project")
    p.add_argument("--limit", type=int, default=10, help="tasks shown per person")
    p = sub.add_parser("ses", help="check SES: sender, quota, last 14 days; --send-test <email>")
    p.add_argument("--send-test", default="", metavar="EMAIL")
    p = sub.add_parser("notifications")
    p.add_argument("--project", required=True)
    p.add_argument("--limit", type=int, default=8)
    a = ap.parse_args()
    if getattr(a, "email", None):
        a.email = a.email.strip().lower()
    try:
        {"status": cmd_status, "make-due": cmd_make_due, "owner-late": cmd_owner_late,
         "eventbridge": cmd_eventbridge, "notifications": cmd_notifications, "ses": cmd_ses,
         "tasks": cmd_tasks}[a.cmd](a)
    except Exception as e:
        if "ResourceNotFoundException" in f"{type(e).__name__} {e}":
            print("\nA DynamoDB table does not exist in this AWS account/region (most likely 'onebox-planning',"
                  " used by updates, check-ins and Replanificación). Create it once with:\n"
                  "    python scripts/dynamo/create_planning_table.py")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
