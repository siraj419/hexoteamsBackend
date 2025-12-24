from fastapi import APIRouter

from .auth import router as auth_router
from .organization import router as organization_router
from .project import router as project_router
from .task import router as task_router
from .file import router as file_router
from .chat import router as chat_router
from .teams import router as teams_router
from .websocket import router as websocket_router
from .time_log import router as time_log_router
from .inbox import router as inbox_router
from .misc import router as misc_router

v1_router = APIRouter()


v1_router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
v1_router.include_router(organization_router, prefix="/organizations", tags=["Organizations"])
v1_router.include_router(project_router, prefix="/projects", tags=["Projects"])
v1_router.include_router(task_router, prefix="/tasks", tags=["Tasks"])
v1_router.include_router(file_router, prefix="/files", tags=["Files"])
v1_router.include_router(chat_router, prefix="/chat", tags=["Chat"])
v1_router.include_router(teams_router, prefix="/teams", tags=["Teams"])
v1_router.include_router(websocket_router, prefix="/ws", tags=["WebSocket"])
v1_router.include_router(time_log_router, prefix="/time-logs", tags=["Time Logs"])
v1_router.include_router(inbox_router, prefix="/inbox", tags=["Inbox"])
v1_router.include_router(misc_router, prefix="/misc", tags=["Misc"])
__all__ = [
    "v1_router"
]