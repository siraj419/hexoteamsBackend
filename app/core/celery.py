from celery import Celery
from app.core.config import Settings

settings = Settings()

celery_app = Celery(
    "app",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

import app.tasks.tasks  # noqa: E402, F401 — register all @celery_app.task in worker and API process
