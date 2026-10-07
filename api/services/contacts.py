"""People the user already works with, for autocomplete.

Every participant of every project the user can see -- the ones they OWN and
the ones they take part in -- plus the OWNERS of the projects shared with
them, deduplicated. These
people are already stored -- nobody reused them, so the frontend asked the
user to type the same email again in every new project, invitation and
document preview.

Only projects the user has access to (own, shared, invited): a contact list
is not a way to discover people from other tenants.
"""
import os

from agent.tools.access import _accessible_project_ids
from agent.tools.db import dynamodb, projects_table
from api.services.projects import normalize_participants

_MAX_PROJECTS = 300   # batch_get_item takes 100 keys per call


def _load_projects(ids: list) -> list:
    items = []
    for i in range(0, len(ids), 100):
        keys = [{"projectId": pid} for pid in ids[i:i + 100]]
        request = {projects_table.table_name: {
            "Keys": keys,
            "ProjectionExpression": "projectId, #n, participants, userId",
            "ExpressionAttributeNames": {"#n": "name"},
        }}
        # UnprocessedKeys is DynamoDB asking us to retry, not a failure.
        for _ in range(5):
            resp = dynamodb.batch_get_item(RequestItems=request)
            items.extend(resp.get("Responses", {}).get(projects_table.table_name, []))
            request = resp.get("UnprocessedKeys") or {}
            if not request:
                break
    return items


# Owner uid -> {"name", "email"}. A project stores its owner only as a
# Cognito sub, and the owner is usually NOT in its own participant list, so
# without this lookup the person who shared the project was never suggested.
_OWNER_CACHE: dict = {}


def _owner_profile(owner_uid: str) -> dict:
    if owner_uid in _OWNER_CACHE:
        return _OWNER_CACHE[owner_uid]
    profile = {"name": "", "email": ""}
    try:
        import boto3
        pool_id = os.environ.get('COGNITO_USER_POOL_ID', 'us-east-1_b76prubhx')
        cip = boto3.client('cognito-idp', region_name='us-east-1')
        attrs = {a['Name']: a.get('Value', '') for a in
                 cip.admin_get_user(UserPoolId=pool_id, Username=owner_uid)
                 .get('UserAttributes', [])}
        profile = {
            "email": (attrs.get('email') or '').strip().lower(),
            "name": (attrs.get('name') or ' '.join(
                x for x in (attrs.get('given_name'), attrs.get('family_name')) if x)
                or '').strip(),
        }
    except Exception as e:
        print(f"[contacts] owner lookup failed for {owner_uid[:8]}…: {e}")
    _OWNER_CACHE[owner_uid] = profile
    return profile


def _key(person: dict) -> str:
    """Same person = same email; without an email, same name."""
    email = (person.get("email") or "").strip().lower()
    return f"e:{email}" if email else f"n:{(person.get('name') or '').strip().lower()}"


def list_contacts(uid: str, user_email: str) -> dict:
    ids = sorted(_accessible_project_ids(uid, user_email))[:_MAX_PROJECTS]
    me = (user_email or "").strip().lower()
    people: dict = {}

    for proj in _load_projects(ids):
        people_here = normalize_participants(proj.get("participants"))
        owner_uid = proj.get("userId") or ""
        if owner_uid and owner_uid != uid:
            owner = _owner_profile(owner_uid)
            if owner["email"] or owner["name"]:
                people_here.append({"name": owner["name"], "email": owner["email"],
                                    "phone": "", "role": "Owner"})
        for p in people_here:
            if not p["name"] and not p["email"]:
                continue                       # a phone alone is not searchable
            if me and p["email"].lower() == me:
                continue                       # do not suggest the user to themself
            k = _key(p)
            entry = people.get(k)
            if entry is None:
                entry = people[k] = {
                    "name": p["name"], "email": p["email"].lower(),
                    "phone": p["phone"], "role": p["role"], "projects": [],
                }
            # Fill gaps from other projects: one project may have the email,
            # another the phone.
            for f in ("name", "phone", "role"):
                if not entry[f] and p[f]:
                    entry[f] = p[f]
            if not entry["email"] and p["email"]:
                entry["email"] = p["email"].lower()
            name = proj.get("name") or ""
            if name and name not in entry["projects"]:
                entry["projects"].append(name)

    # A name-only entry whose name also appears WITH an email is the same
    # person seen before their email was known: keep the richer one.
    with_email = {c["name"].strip().lower() for c in people.values() if c["email"]}
    contacts = [c for c in people.values()
                if c["email"] or c["name"].strip().lower() not in with_email]
    contacts.sort(key=lambda c: (-len(c["projects"]), c["name"].lower()))
    return {"contacts": contacts, "count": len(contacts)}
