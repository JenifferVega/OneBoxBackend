"""DEVELOPMENT ONLY: see the app as another user ("view as").

The frontend sends, besides the normal identity headers:
    x-view-as          email of the user to act as
    x-view-as-uid      (optional) their user id; looked up in Cognito if absent
    x-view-as-token    must equal VIEW_AS_TOKEN in this server's environment

When the token matches, this middleware rewrites x-user-id / x-user-email
BEFORE any endpoint reads them, so every route, permission check and agent
tool runs as that user. Every switched request is logged (event view_as)
with the real user's id.

Off unless VIEW_AS_TOKEN is set: a request asking to view-as on a server
without it gets 403, never a silent switch. REMOVE THIS (and VIEW_AS_TOKEN)
before full production -- see also the frontend's src/devViewAs.ts.
"""
import hmac
import os

from agent import obs

_UID_CACHE: dict = {}


def _uid_for(email: str) -> str:
    """The Cognito sub of this email, or a stable placeholder for someone
    without an account (a member who never signed up)."""
    if email in _UID_CACHE:
        return _UID_CACHE[email]
    uid = ""
    try:
        import boto3
        pool = os.environ.get("COGNITO_USER_POOL_ID", "us-east-1_b76prubhx")
        cip = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        users = cip.list_users(UserPoolId=pool, Filter=f'email = "{email.replace(chr(34), "")}"', Limit=1).get("Users", [])
        for a in (users[0].get("Attributes", []) if users else []):
            if a.get("Name") == "sub":
                uid = a.get("Value", "")
    except Exception as e:
        print(f"[view_as] Cognito lookup failed for {email}: {e}")
    uid = uid or f"viewas-{email}"
    _UID_CACHE[email] = uid
    return uid


async def _deny(send, detail: str):
    import json
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class ViewAsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        target = (headers.get("x-view-as") or "").strip().lower()
        if not target:
            return await self.app(scope, receive, send)

        expected = os.environ.get("VIEW_AS_TOKEN", "")
        given = headers.get("x-view-as-token", "")
        if not expected:
            return await _deny(send, "view-as is disabled on this server (VIEW_AS_TOKEN not set)")
        if not hmac.compare_digest(given, expected):
            return await _deny(send, "view-as token is invalid")

        uid = (headers.get("x-view-as-uid") or "").strip() or _uid_for(target)
        obs.warn("view_as", real_uid=headers.get("x-user-id", ""), real_email=headers.get("x-user-email", ""),
                 as_uid=uid, as_email=target, path=scope.get("path", ""))
        drop = {b"x-user-id", b"x-user-email", b"x-view-as", b"x-view-as-uid", b"x-view-as-token"}
        new = [(k, v) for k, v in scope["headers"] if k.lower() not in drop]
        new += [(b"x-user-id", uid.encode()), (b"x-user-email", target.encode())]
        return await self.app({**scope, "headers": new}, receive, send)
