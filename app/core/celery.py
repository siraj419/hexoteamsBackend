from celery import Celery
from app.core.config import Settings

settings = Settings()

celery_app = Celery(
    "app",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
)

# Import tasks to register them with celery
import app.tasks.tasks  # noqa: E402, F401
