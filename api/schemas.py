"""Modelos Pydantic de request/response de la API."""
from typing import List, Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[dict]] = []
    debug: bool = False  # si True: dry-run, no modifica DB, retorna debug_info
    session_id: Optional[str] = None  # ID de sesión MCP (para cache dry-run por sesión)


class ChatResponse(BaseModel):
    response: str
    toolsUsed: List[str] = []
    debug_info: Optional[dict] = None  # solo presente cuando debug=True


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""
    type: str = "Otro"
    participants: Optional[List[dict]] = []
    channels: Optional[List[str]] = ["Gmail"]
    timing: Optional[str] = ""  # Plazo del proyecto (libre, ej: "8 semanas", "30/06/2026", "Q3 2026")
    deliveryDate: Optional[str] = ""  # Fecha de entrega ISO (opcional)


class UpdateParticipantsRequest(BaseModel):
    participants: List[dict]


class InviteRequest(BaseModel):
    # Email y/o teléfono — al menos uno requerido (validado en el servicio)
    email: Optional[str] = ""
    phone: Optional[str] = ""
    # Nombre y rol opcionales para personalizar el participant que se guarda
    name: Optional[str] = ""
    role: Optional[str] = ""
    # Si False, solo registra el contacto en participants[] sin enviar notificación
    send_notification: Optional[bool] = True


class RemoveParticipantRequest(BaseModel):
    """Identifica al participante a eliminar por uno de estos campos.
    Prioridad de matching: email > phone > name. El primero que coincida gana."""
    email: Optional[str] = ""
    phone: Optional[str] = ""
    name: Optional[str] = ""


class CreateTaskRequest(BaseModel):
    text: str
    assigned_to: str = ""
    status: str = "pending"
    description: str = ""
    start_date: Optional[str] = None   # YYYY-MM-DD (opcional)
    due_date: Optional[str] = None     # YYYY-MM-DD (opcional)
    parent_task_id: Optional[str] = None  # taskId del padre (subtarea) o None


class UpdateTaskRequest(BaseModel):
    text: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    description: Optional[str] = None
    blocked_reason: Optional[str] = None  # Motivo del bloqueo (opcional)
    start_date: Optional[str] = None      # YYYY-MM-DD
    due_date: Optional[str] = None        # YYYY-MM-DD
    parent_task_id: Optional[str] = None  # mover tarea a/desde subtarea (string vacío = raíz)


class AssignRequest(BaseModel):
    projectId: str


class AnalyzeTextPreviewRequest(BaseModel):
    text: str
    source: Optional[str] = "paste"


class CreateProjectFromDraftRequest(BaseModel):
    draftId: str
    name: str
    type: Optional[str] = "Otro"
    description: str
    channels: List[str] = []
    emails: Optional[List[str]] = []
    phones: Optional[List[str]] = []
    timing: Optional[str] = ""
    deliveryDate: Optional[str] = ""
    # Participantes detectados por IA: cada uno con {name, email, phone, role}
    # Permite preservar el nombre real (Kevin/Mateo) en lugar de usar el email como nombre.
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
