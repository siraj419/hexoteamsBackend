import importlib

from .config import Settings

settings = Settings()

__all__ = [
    "settings",
    "celery_app",
    "mailer",
]


def __getattr__(name: str):
    if name == "celery_app":
        mod = importlib.import_module("app.core.celery")
        return mod.celery_app
    if name == "mailer":
        mod = importlib.import_module("app.core.email")
        return mod.mailer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")