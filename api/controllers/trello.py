"""Trello endpoints: authorization, token capture, status, disconnect, boards."""
from fastapi import APIRouter, Body, Header

from api.deps import require_uid
from api.services import trello as trello_service

router = APIRouter()


@router.get("/api/trello/auth")
async def trello_auth(x_user_id: str = Header(default="")):
    """Return the Trello authorization URL for this user to open."""
    uid = require_uid(x_user_id)
    return trello_service.build_auth_url(uid)


@router.post("/api/trello/token")
async def trello_save_token(
    x_user_id: str = Header(default=""),
    token: str = Body(..., embed=True),
):
    """Store the token Trello handed the user after they approved access.

    Unlike Gmail there is no code exchange: Trello returns the token in the
    URL fragment, so the frontend reads window.location.hash and POSTs it here.
    """
    uid = require_uid(x_user_id)
    return trello_service.save_token(uid, token)


@router.get("/api/trello/status")
async def trello_status(x_user_id: str = Header(default="")):
    uid = require_uid(x_user_id)
    return trello_service.get_status(uid)


@router.delete("/api/trello/disconnect")
async def trello_disconnect(x_user_id: str = Header(default="")):
    uid = require_uid(x_user_id)
    return trello_service.disconnect(uid)


@router.get("/api/trello/boards")
async def trello_boards(x_user_id: str = Header(default="")):
    """Boards the user can write to, each with its lists, for the UI picker."""
    uid = require_uid(x_user_id)
    boards = trello_service.list_boards(uid)
    return {"count": len(boards), "boards": boards}


@router.get("/api/trello/boards/{board_id}/lists")
async def trello_board_lists(board_id: str, x_user_id: str = Header(default="")):
    uid = require_uid(x_user_id)
    lists = trello_service.list_board_lists(uid, board_id)
    return {"count": len(lists), "lists": lists}
