"""API routers. `all_routers` is registered in api.app.create_app()."""
from api.controllers.attachments import router as attachments_router
from api.controllers.chat import router as chat_router
from api.controllers.checkin import router as checkin_router
from api.controllers.updates import router as updates_router
from api.controllers.documents import router as documents_router
from api.controllers.gmail import router as gmail_router
from api.controllers.inbox import router as inbox_router
from api.controllers.insights import router as insights_router
from api.controllers.phones import router as phones_router
from api.controllers.planning import router as planning_router
from api.controllers.planning_debug import router as planning_debug_router
from api.controllers.model_lab import router as model_lab_router
from api.controllers.projects import router as projects_router
from api.controllers.scheduled import router as scheduled_router
from api.controllers.system import router as system_router
from api.controllers.team import router as team_router
from api.controllers.tasks import router as tasks_router
from api.controllers.trello import router as trello_router
from api.controllers.users import router as users_router
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
    trello_router,
    users_router,
    planning_router,
    checkin_router,
    updates_router,
    planning_debug_router,
    model_lab_router,
    system_router,
    team_router,
]
