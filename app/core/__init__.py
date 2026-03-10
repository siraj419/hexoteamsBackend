from .config import Settings
from .supabase import supabase, supabase_auth_client
from .celery import celery_app
from .email import mailer



settings = Settings()

__all__ = [
    "settings",
    "supabase",
    "supabase_auth_client",
    "celery_app",
    "mailer",
]