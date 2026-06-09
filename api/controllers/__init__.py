"""Routers de la API. `all_routers` se registra en api.app.create_app()."""
from api.controllers.attachments import router as attachments_router
from api.controllers.chat import router as chat_router
from api.controllers.documents import router as documents_router
from api.controllers.gmail import router as gmail_router
from api.controllers.inbox import router as inbox_router
from api.controllers.insights import router as insights_router
from api.controllers.phones import router as phones_router
from api.controllers.projects import router as projects_router
from api.controllers.scheduled import router as scheduled_router
from api.controllers.tasks import router as tasks_router
from api.controllers.whatsapp import router as whatsapp_router

all_routers = [
    chat_router,
    projects_router,
    documents_router,
    attachments_router,
    tasks_router,
    inbox_router,
    insights_router,
    gmail_router,
    phones_router,
    whatsapp_router,
    scheduled_router,
]
