"""Sync SQLAlchemy URL (psycopg2) for SyncSessionLocal and Alembic."""

from app.core.config import normalize_async_database_url, settings


def get_sync_database_url() -> str:
    u = normalize_async_database_url(settings.DATABASE_URL)
    if "+asyncpg" in u:
        return u.replace("postgresql+asyncpg://", "postgresql://", 1)
    return u
