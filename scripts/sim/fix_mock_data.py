"""Make the MOCK data consistent so the update loop can be tested on it.

    python scripts/sim/fix_mock_data.py                      # dry run: prints the plan, writes nothing
    python scripts/sim/fix_mock_data.py --apply              # writes (and saves a backup first)
    python scripts/sim/fix_mock_data.py --restore scripts/sim/backups/fix-<ts>.json

Options:
    --project <id>                 only this project
    --name EMAIL=NAME              canonical name for a person (repeatable). Default: the
                                   name used most often for that email across projects
                                   (names equal to the email's local part do not count)
    --me EMAIL --me-tasks N        make sure this person has at least N open tasks in
                                   every project where they participate (default 2)

What it fixes, per active project:
  1. NAMES  one person = one name: participants with the same email get the
            canonical name, and tasks assigned under the old name follow.
  2. PEOPLE assignees that are not participants ("Rafael Sardinha", "BION") are
            added as participants without email (contacts without an account).
  3. OWNERS open tasks without assignee are spread round-robin over the
            participants WITH email (people who can log in and give updates);
            if there are none, over any participant; with no participants, left
            as they are (reported).
Only `participants` of projects and `assignedTo` of tasks change. The backup
holds both as they were, for --restore.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from agent.planning.sources import match_person, norm, normalize_participant  # noqa: E402
from api.services import checkin  # noqa: E402

NAME_KEYS = ("name", "nombre", "fullName", "full_name")
CLOSED = ("archived", "completed", "closed")


def _set_name(p, new: str):
    if not isinstance(p, dict):
        return {"name": new}
    p = dict(p)
    key = next((k for k in p if k in NAME_KEYS or str(k).lower() in NAME_KEYS), "name")
    p[key] = new
    return p


def _local(email: str) -> str:
    return norm(email.split("@")[0]) if email else ""


def canonical_names(projects, overrides: dict) -> dict:
    seen = defaultdict(Counter)
    for proj in projects:
        for p in proj.get("participants") or []:
            q = normalize_participant(p)
            if q["email"] and q["name"] and norm(q["name"]) != _local(q["email"]):
                seen[q["email"]][q["name"]] += 1
    out = {}
    for email, names in seen.items():
        out[email] = sorted(names.items(), key=lambda kv: (-kv[1], -len(kv[0])))[0][0]
    out.update(overrides)
    return out


def plan(projects, tasks_by_project, names: dict, me: str, me_tasks: int, owner_of):
    """[(project, new_participants or None, {taskId: (old, new)}, notes)]"""
    out = []
    for proj in projects:
        pid = proj["projectId"]
        notes = []
        parts_raw = list(proj.get("participants") or [])
        parts = [normalize_participant(p) for p in parts_raw]
        tasks = tasks_by_project[pid]
        changes = {}
        new_raw = list(parts_raw)
        # 1. names
        for i, q in enumerate(parts):
            want = names.get(q["email"])
            if q["email"] and want and q["name"] != want:
                old = q["name"]
                new_raw[i] = _set_name(parts_raw[i], want)
                for t in tasks:
                    a = (t.get("assignedTo") or "").strip()
                    if a and (norm(a) == norm(old) or match_person(a, [q]) is q):
                        changes[t["taskId"]] = (a, want)
                notes.append(f"rename '{old}' -> '{want}' ({q['email']})")
        cur = [normalize_participant(p) for p in new_raw]
        # 2. assignees that are not participants
        for t in tasks:
            if t.get("status") not in checkin.OPEN:
                continue
            a = changes.get(t["taskId"], (None, (t.get("assignedTo") or "").strip()))[1]
            if not a or norm(a) in ("sin asignar", "unassigned", "-", "none"):
                continue
            if not (match_person(a, cur) or next((p for p in cur if p["email"] and p["email"] == a.lower()), None)):
                new_raw.append({"name": a, "role": "Contacto (sin cuenta)"})
                cur.append(normalize_participant(new_raw[-1]))
                notes.append(f"add participant '{a}' (assigned tasks, no account)")
        # 3. unassigned open tasks
        unassigned = [t for t in tasks if t.get("status") in checkin.OPEN and
                      norm(changes.get(t["taskId"], (None, t.get("assignedTo") or ""))[1]) in ("", "sin asignar",
                                                                                                "unassigned")]
        owner = owner_of(proj)
        pool = [p for p in cur if p["email"] and p["email"] != owner] or [p for p in cur if p["email"]] or cur
        mine = next((p for p in cur if me and p["email"] == me), None)
        if mine:
            have = sum(1 for t in tasks if t.get("status") in checkin.OPEN and
                       match_person(changes.get(t["taskId"], (None, t.get("assignedTo") or ""))[1] or "", cur) is mine)
            for t in unassigned[: max(0, me_tasks - have)]:
                changes[t["taskId"]] = (t.get("assignedTo") or "", mine["name"])
            unassigned = [t for t in unassigned if t["taskId"] not in changes]
        if unassigned and not pool:
            notes.append(f"{len(unassigned)} open tasks without assignee and NO participants: left as they are")
        elif unassigned:
            for i, t in enumerate(unassigned):
                changes[t["taskId"]] = (t.get("assignedTo") or "", pool[i % len(pool)]["name"])
            notes.append(f"assign {len(unassigned)} unassigned open tasks over: "
                         + ", ".join(p["name"] for p in pool))
        out.append((proj, new_raw if new_raw != parts_raw else None, changes, notes))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--project", default="")
    ap.add_argument("--name", action="append", default=[], metavar="EMAIL=NAME")
    ap.add_argument("--me", default="")
    ap.add_argument("--me-tasks", type=int, default=2)
    ap.add_argument("--restore", default="")
    a = ap.parse_args()
    from agent.tools import projects_table, tasks_table
    from api.deps import scan_all_pages

    if a.restore:
        data = json.loads(Path(a.restore).read_text(encoding="utf-8"))
        for pid, parts in data["participants"].items():
            projects_table.update_item(Key={"projectId": pid}, UpdateExpression="SET participants = :p",
                                       ExpressionAttributeValues={":p": parts})
        for key, old in data["assignedTo"].items():
            pid, tid = key.split("|", 1)
            tasks_table.update_item(Key={"projectId": pid, "taskId": tid}, UpdateExpression="SET assignedTo = :a",
                                    ExpressionAttributeValues={":a": old})
        print(f"Restored {len(data['participants'])} projects and {len(data['assignedTo'])} tasks.")
        return

    overrides = {}
    for kv in a.name:
        email, _, name = kv.partition("=")
        overrides[email.strip().lower()] = name.strip()
    allp = [p for p in scan_all_pages(projects_table) if p.get("status") not in CLOSED]
    names = canonical_names(allp, overrides)
    projects = [p for p in allp if not a.project or p["projectId"] == a.project]
    tasks_by_project = {p["projectId"]: checkin._project_tasks(p["projectId"]) for p in projects}
    owners = {}

    def owner_of(proj):
        uid = proj.get("userId", "")
        if uid not in owners:
            owners[uid] = checkin._owner_email(uid)
        return owners[uid]

    steps = plan(projects, tasks_by_project, names, a.me.strip().lower(), a.me_tasks, owner_of)
    print("Canonical names: " + "; ".join(f"{e} = {n}" for e, n in sorted(names.items())))
    backup = {"participants": {}, "assignedTo": {}}
    n_tasks = 0
    for proj, new_parts, changes, notes in steps:
        if not new_parts and not changes:
            continue
        print(f"\n== {proj.get('name')} ({proj['projectId']})")
        for n in notes:
            print(f"   - {n}")
        texts = {t["taskId"]: t.get("text", "") for t in tasks_by_project[proj["projectId"]]}
        for tid, (old, new) in list(changes.items())[:40]:
            print(f"     task {(texts.get(tid) or '')[:55]!r}: '{old}' -> '{new}'")
        n_tasks += len(changes)
        if new_parts:
            backup["participants"][proj["projectId"]] = proj.get("participants") or []
        for tid, (old, _new) in changes.items():
            backup["assignedTo"][f"{proj['projectId']}|{tid}"] = old
    if not a.apply:
        print(f"\nDRY RUN: {len(backup['participants'])} projects / {n_tasks} tasks would change. "
              "Run again with --apply to write.")
        return
    out = ROOT / "scripts" / "sim" / "backups" / f"fix-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(backup, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nBackup: {out.relative_to(ROOT)}")
    for proj, new_parts, changes, _notes in steps:
        if new_parts:
            projects_table.update_item(Key={"projectId": proj["projectId"]}, UpdateExpression="SET participants = :p",
                                       ExpressionAttributeValues={":p": new_parts})
        for tid, (_old, new) in changes.items():
            tasks_table.update_item(Key={"projectId": proj["projectId"], "taskId": tid},
                                    UpdateExpression="SET assignedTo = :a", ExpressionAttributeValues={":a": new})
    print(f"Applied. Undo with: python scripts/sim/fix_mock_data.py --restore {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
