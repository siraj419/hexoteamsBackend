from .config import Settings
from .supabase import supabase
from .celery import celery_app
from .email import mailer



settings = Settings()

__all__ = [
    "settings",
    "supabase",
    "celery_app",
    "mailer",
]