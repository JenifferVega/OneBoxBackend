"""Pydantic request/response models for the API."""
from typing import List, Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[dict]] = []
    debug: bool = False  # if True: dry-run, does not modify DB, returns debug_info
    session_id: Optional[str] = None  # MCP session ID (for per-session dry-run cache)


class ChatResponse(BaseModel):
    response: str
    toolsUsed: List[str] = []
    debug_info: Optional[dict] = None  # only present when debug=True


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""
    type: str = "Other"
    participants: Optional[List[dict]] = []
    channels: Optional[List[str]] = ["Gmail"]
    timing: Optional[str] = ""  # Project timeframe (free text, e.g. "8 weeks", "30/06/2026", "Q3 2026")
    deliveryDate: Optional[str] = ""  # ISO delivery date (optional)


class UpdateProjectRequest(BaseModel):
    # All optional — only fields present in the body are updated.
    # The frontend only sends the fields that changed (edit modal).
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None  # "active" | "paused" | "finished"
    deliveryDate: Optional[str] = None
    timing: Optional[str] = None


class UpdateParticipantsRequest(BaseModel):
    participants: List[dict]


class InviteRequest(BaseModel):
    # Email and/or phone — at least one required (validated in the service)
    email: Optional[str] = ""
    phone: Optional[str] = ""
    # Name and role optional to personalize the participant record saved
    name: Optional[str] = ""
    role: Optional[str] = ""
    # If False, only records the contact in participants[] without sending a notification
    send_notification: Optional[bool] = True


class RemoveParticipantRequest(BaseModel):
    """Identifies the participant to remove by one of these fields.
    Matching priority: email > phone > name. First match wins."""
    email: Optional[str] = ""
    phone: Optional[str] = ""
    name: Optional[str] = ""


class CreateTaskRequest(BaseModel):
    text: str
    assigned_to: str = ""
    status: str = "pending"
    description: str = ""
    start_date: Optional[str] = None   # YYYY-MM-DD (optional)
    due_date: Optional[str] = None     # YYYY-MM-DD (optional)
    parent_task_id: Optional[str] = None  # parent taskId (subtask) or None


class UpdateTaskRequest(BaseModel):
    text: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    description: Optional[str] = None
    blocked_reason: Optional[str] = None  # Blocking reason (optional)
    start_date: Optional[str] = None      # YYYY-MM-DD
    due_date: Optional[str] = None        # YYYY-MM-DD
    parent_task_id: Optional[str] = None  # move task to/from subtask (empty string = root)


class AssignRequest(BaseModel):
    projectId: str


class AnalyzeTextPreviewRequest(BaseModel):
    text: str
    source: Optional[str] = "paste"


class CreateProjectFromDraftRequest(BaseModel):
    draftId: str
    name: str
    type: Optional[str] = "Other"
    description: str
    # Full original text (transcript/paste) so the insights analysis
    # sees the real material, not the short description. Optional (backward-compatible).
    sourceText: Optional[str] = ""
    channels: List[str] = []
    emails: Optional[List[str]] = []
    phones: Optional[List[str]] = []
    timing: Optional[str] = ""
    deliveryDate: Optional[str] = ""
    # AI-detected participants: each one with {name, email, phone, role}
    # Lets us preserve the real name (Kevin/Mateo) instead of using the email as name.
    detectedParticipants: Optional[List[dict]] = []


class AnalyzeTextRequest(BaseModel):
    text: str
    source: Optional[str] = "paste"  # "paste", "whatsapp", "gmail", "manual"


class CreateProjectFromTextRequest(BaseModel):
    text: str
    name: Optional[str] = None
    channels: Optional[List[str]] = None
    source: Optional[str] = "paste"


class LinkPhoneRequest(BaseModel):
    phoneNumber: str
