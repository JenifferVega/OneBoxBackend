"""Endpoint for the AI insights/actions feed."""
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from api.deps import require_uid
from api.services import insights as insights_service

router = APIRouter()


@router.get("/api/insights")
async def get_insights(type: Optional[str] = Query(None), x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """List AI insights/actions for ALL projects the user has access to
    (own + accepted invitations). Optionally filters by type."""
    uid = require_uid(x_user_id)
    try:
        return insights_service.list_insights(uid, x_user_email, type)
    except Exception as e:
        print(f"[API] Error in get_insights: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
