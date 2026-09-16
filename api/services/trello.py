"""Trello integration: authorization, boards/lists lookup and card creation.

AUTH MODEL (why this is much shorter than gmail.py)
---------------------------------------------------
Trello does not need an OAuth code exchange. You send the user to
`/1/authorize`, they approve, and Trello hands THEM a token which they give
back to us. There is no refresh cycle: with `expiration=never` the token
stays valid until the user revokes it from their Trello account settings.

So the whole flow is:
    build_auth_url(uid)  ->  user approves  ->  save_token(uid, token)

The API key is public by design; the token is NOT -- it grants access to that
user's entire Trello account, so it lives in DynamoDB and never reaches the
frontend after being saved.
"""
import os
from datetime import datetime

import requests
from fastapi import HTTPException
from urllib.parse import quote

from api.deps import user_tokens_table

TRELLO_API = "https://api.trello.com/1"

# From trello.com/apps/admin -> your Power-Up -> API Key tab.
TRELLO_API_KEY = os.environ.get("TRELLO_API_KEY", "")
# Only needed to verify webhook signatures (not used yet, one-way sync).
TRELLO_API_SECRET = os.environ.get("TRELLO_API_SECRET", "")
# Where Trello sends the user back with the token in the URL fragment.
# OPTIONAL on purpose: leave it unset and Trello shows the token on screen for
# the user to copy. That makes the first connection testable with nothing but
# a browser and curl -- no frontend callback route needed yet.
TRELLO_RETURN_URL = os.environ.get("TRELLO_RETURN_URL", "").strip()

TIMEOUT = 15


# ── Authorization ───────────────────────────────────────────────────────────

def build_auth_url(uid: str) -> dict:
    """URL the user opens to grant OneBox access to their Trello account.

    Trello returns the token in the URL FRAGMENT (#token=...), which never
    reaches the server. The frontend must read it from window.location.hash
    and POST it to /api/trello/token.
    """
    if not TRELLO_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="TRELLO_API_KEY is not set. Create a Power-Up at "
                   "trello.com/apps/admin and copy its API Key.",
        )
    params = (
        f"expiration=never&scope=read,write&response_type=token"
        f"&name=OneBox&key={TRELLO_API_KEY}"
    )
    if TRELLO_RETURN_URL:
        params += f"&return_url={quote(TRELLO_RETURN_URL, safe='')}"
    return {
        "authUrl": f"https://trello.com/1/authorize?{params}",
        "mode": "redirect" if TRELLO_RETURN_URL else "manual",
        "hint": (
            "Trello will redirect to the return URL with the token in the "
            "fragment (#token=...); read it with window.location.hash."
            if TRELLO_RETURN_URL else
            "No return URL configured: Trello will show the token on screen. "
            "Copy it and POST it to /api/trello/token."
        ),
    }


def save_token(uid: str, token: str) -> dict:
    """Validate the token against Trello and store it for this user.

    We call /members/me before saving. Storing an unvalidated token is how you
    get a user who looks connected and silently fails on every later call.
    """
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token required")

    resp = requests.get(
        f"{TRELLO_API}/members/me",
        params={"key": TRELLO_API_KEY, "token": token, "fields": "username,fullName"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Trello rejected the token ({resp.status_code}): {resp.text[:200]}",
        )
    me = resp.json()

    # update_item, NOT put_item: this row is shared with Gmail. put_item would
    # replace the whole item and silently drop gmailRefreshToken.
    user_tokens_table.update_item(
        Key={"userId": uid},
        UpdateExpression=(
            "SET trelloToken = :t, trelloUsername = :u, "
            "trelloConnected = :c, trelloConnectedAt = :d"
        ),
        ExpressionAttributeValues={
            ":t": token,
            ":u": me.get("username", ""),
            ":c": True,
            ":d": datetime.utcnow().isoformat(),
        },
    )
    print(f"[Trello] connected uid={uid[:8]}... username={me.get('username', '')}")
    return {"success": True, "username": me.get("username", ""),
            "fullName": me.get("fullName", "")}


def get_status(uid: str) -> dict:
    try:
        item = user_tokens_table.get_item(Key={"userId": uid}).get("Item") or {}
        if item.get("trelloConnected") and item.get("trelloToken"):
            return {"connected": True, "username": item.get("trelloUsername", ""),
                    "connectedAt": item.get("trelloConnectedAt", "")}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def disconnect(uid: str) -> dict:
    """Remove ONLY the Trello attributes.

    Deliberately not delete_item: that would also wipe the user's Gmail
    tokens, which live in this same row.
    """
    try:
        user_tokens_table.update_item(
            Key={"userId": uid},
            UpdateExpression=(
                "REMOVE trelloToken, trelloUsername, trelloConnected, trelloConnectedAt"
            ),
        )
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _creds(uid: str) -> dict:
    """Credentials for this user, or a clear error if they are not connected."""
    item = user_tokens_table.get_item(Key={"userId": uid}).get("Item") or {}
    token = item.get("trelloToken", "")
    if not token:
        raise HTTPException(
            status_code=400,
            detail="This user has not connected Trello. Call /api/trello/auth first.",
        )
    return {"key": TRELLO_API_KEY, "token": token}


# ── Reads ───────────────────────────────────────────────────────────────────

def list_boards(uid: str) -> list:
    """Boards the user can write to."""
    resp = requests.get(
        f"{TRELLO_API}/members/me/boards",
        params={**_creds(uid), "fields": "name,url,closed", "filter": "open"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {"boardId": b["id"], "name": b.get("name", ""), "url": b.get("url", "")}
        for b in resp.json() if not b.get("closed")
    ]


def list_board_lists(uid: str, board_id: str) -> list:
    """Lists (columns) of a board. In Trello the list IS the status."""
    resp = requests.get(
        f"{TRELLO_API}/boards/{board_id}/lists",
        params={**_creds(uid), "fields": "name,closed"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return [
        {"listId": l["id"], "name": l.get("name", "")}
        for l in resp.json() if not l.get("closed")
    ]


def list_cards(uid: str, list_id: str) -> list:
    """Every card in a list, in ONE request.

    This is what makes comparing affordable: the alternative is a GET per
    task, and the Trello rate limit already bites at four tasks. `fields`
    is narrowed to what the comparison actually reads.
    """
    resp = requests.get(
        f"{TRELLO_API}/lists/{list_id}/cards",
        params={**_creds(uid),
                "fields": "name,desc,due,idList,dateLastActivity,closed"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Trello list read failed ({resp.status_code}): {resp.text[:200]}",
        )
    return [
        {"cardId": c.get("id", ""), "name": c.get("name", ""),
         "desc": c.get("desc", ""), "due": (c.get("due") or "")[:10],
         "listId": c.get("idList", ""), "closed": bool(c.get("closed")),
         "lastActivity": c.get("dateLastActivity", "")}
        for c in resp.json()
    ]


# ── Writes ──────────────────────────────────────────────────────────────────

def create_card(uid: str, list_id: str, name: str, desc: str = "",
                due: str = "") -> dict:
    """Create a card. `due` is ISO-8601 (YYYY-MM-DD is accepted)."""
    body = {**_creds(uid), "idList": list_id, "name": name[:16384]}
    if desc:
        body["desc"] = desc[:16384]
    if due:
        body["due"] = due
    resp = requests.post(f"{TRELLO_API}/cards", params=body, timeout=TIMEOUT)
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"Trello card creation failed ({resp.status_code}): {resp.text[:200]}",
        )
    card = resp.json()
    return {"cardId": card.get("id", ""), "url": card.get("shortUrl", "")}


def update_card(uid: str, card_id: str, name: str = "", desc: str = "",
                due: str = "") -> dict:
    """Update an existing card's content. Only the fields given are sent.

    This is what makes a second export an UPDATE instead of a duplicate: the
    task already has a card, so the card is brought in line with the task
    rather than skipped or created again.

    A card the user deleted in Trello answers 404. That is not an error worth
    failing the whole export over -- it is reported so the caller can clear
    the stale link and create the card anew.
    """
    body = {**_creds(uid)}
    if name:
        body["name"] = name[:16384]
    if desc:
        body["desc"] = desc[:16384]
    if due:
        body["due"] = due
    resp = requests.put(f"{TRELLO_API}/cards/{card_id}", params=body,
                        timeout=TIMEOUT)
    if resp.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Trello card no longer exists (deleted in Trello)",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Trello card update failed ({resp.status_code}): {resp.text[:200]}",
        )
    card = resp.json()
    return {"cardId": card.get("id", ""), "url": card.get("shortUrl", "")}


def create_board(uid: str, name: str, lists: list = None) -> dict:
    """Create a board and, optionally, its lists in the given order.

    defaultLists is forced to "false" and the lists are created explicitly.
    That is the point of creating the board from OneBox: when WE define the
    columns, the list-to-status mapping stops being a guess about someone
    else's naming convention and becomes something we recorded.

    Trello creates lists newest-first, so they are sent in reverse to end up
    on the board in the order the caller asked for.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Board name required")

    resp = requests.post(
        f"{TRELLO_API}/boards/",
        params={**_creds(uid), "name": name[:16384], "defaultLists": "false"},
        timeout=TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"Trello board creation failed ({resp.status_code}): {resp.text[:200]}",
        )
    board = resp.json()
    board_id = board.get("id", "")

    created_lists = []
    for list_name in reversed(list(lists or [])):
        list_name = (list_name or "").strip()
        if not list_name:
            continue
        lr = requests.post(
            f"{TRELLO_API}/lists",
            params={**_creds(uid), "name": list_name[:16384], "idBoard": board_id},
            timeout=TIMEOUT,
        )
        if lr.status_code not in (200, 201):
            # The board already exists; report the partial state instead of
            # raising, so the caller knows what it has to work with.
            created_lists.append({"name": list_name, "error": lr.text[:120]})
            continue
        created_lists.append({"listId": lr.json().get("id", ""), "name": list_name})
    created_lists.reverse()

    print(f"[Trello] board created id={board_id} name={name!r} "
          f"lists={[l.get('name') for l in created_lists]}")
    return {
        "boardId": board_id,
        "name": board.get("name", name),
        "url": board.get("shortUrl", board.get("url", "")),
        "lists": created_lists,
    }
