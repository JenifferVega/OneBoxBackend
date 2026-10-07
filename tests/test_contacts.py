#!/usr/bin/env python3
"""Contacts for autocomplete: people from the user's own projects.

    python tests/test_contacts.py
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.services.contacts as contacts  # noqa: E402

failures = []
print()


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


PROJECTS = {
    "p1": {"projectId": "p1", "name": "Alpha", "participants": [
        {"name": "Laura Gomez", "email": "Laura@Acme.com", "role": "Lead"},
        {"nombre": "Daniel Rojas", "rol": "Dev"},                 # old spelling, no email
        {"name": "Me", "email": "me@acme.com"},
    ]},
    "p2": {"projectId": "p2", "name": "Beta", "userId": "owner-2", "participants": [
        {"name": "Laura G.", "email": "laura@acme.com", "phone": "+57300"},
        {"name": "Daniel Rojas", "email": "daniel@acme.com"},
    ]},
    "p9": {"projectId": "p9", "name": "Someone else's", "participants": [
        {"name": "Stranger", "email": "x@other.com"},
    ]},
}


class FakeDynamo:
    def batch_get_item(self, RequestItems):
        keys = RequestItems["projects"]["Keys"]
        return {"Responses": {"projects": [PROJECTS[k["projectId"]] for k in keys]}}


class FakeTable:
    table_name = "projects"


contacts._accessible_project_ids = lambda uid, email: {"p1", "p2"}
PROJECTS["p1"]["userId"] = "u1"                     # mine: I own it
contacts._OWNER_CACHE["owner-2"] = {"name": "Olga Duena", "email": "olga@acme.com"}
contacts.dynamodb = FakeDynamo()
contacts.projects_table = FakeTable()

out = contacts.list_contacts("u1", "me@acme.com")
by_email = {c["email"]: c for c in out["contacts"]}

out = contacts.list_contacts("u1", "me@acme.com")
by_email = {c["email"]: c for c in out["contacts"]}
check("x@other.com" not in by_email,
      "only people from projects the user can access")
check("me@acme.com" not in by_email, "the user is not suggested to themself")
laura = by_email.get("laura@acme.com") or {}
check(len([c for c in out["contacts"] if c["email"] == "laura@acme.com"]) == 1,
      "the same email in two projects is ONE contact (case-insensitive)")
check(laura.get("phone") == "+57300" and laura.get("role") == "Lead",
      "details missing in one project are filled in from another")
check(set(laura.get("projects", [])) == {"Alpha", "Beta"},
      "and it lists the projects the person is in")
check([c for c in out["contacts"] if c["name"] == "Daniel Rojas"
       and not c["email"]] == [],
      "a name-only entry is dropped when the same name exists with an email")
olga = by_email.get("olga@acme.com") or {}
check(olga.get("role") == "Owner" and olga.get("projects") == ["Beta"],
      "the OWNER of a project shared with me is suggested, though not a participant")
check(not any(c.get("role") == "Owner" and c["email"] == "me@acme.com"
              for c in out["contacts"]),
      "my own projects do not add me as their owner")
check(out["contacts"][0]["email"] == "laura@acme.com",
      "people in more projects come first")

print("\n─ projects shared with me past the first scan page ──────────")
import agent.tools.access as access  # noqa: E402


class PagedProjects:
    """Two scan pages, as DynamoDB returns them past 1 MB."""
    def query(self, **_):
        return {"Items": [{"projectId": "mine"}]}

    def scan(self, **kw):
        if "ExclusiveStartKey" not in kw:
            return {"Items": [{"projectId": "a", "participants": [{"email": "x@y.com"}]}],
                    "LastEvaluatedKey": {"projectId": "a"}}
        return {"Items": [{"projectId": "late", "participants": [{"email": "ME@acme.com"}]}]}


class NoInvites:
    def query(self, **_):
        return {"Items": []}


real = (access.projects_table, access.invitations_table)
access.projects_table, access.invitations_table = PagedProjects(), NoInvites()
try:
    ids = access._accessible_project_ids("u1", "me@acme.com")
finally:
    access.projects_table, access.invitations_table = real
check("late" in ids, "a project on the SECOND scan page is found")
check("mine" in ids and "a" not in ids, "own projects in, other people's out")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("contacts: all clear\n")
