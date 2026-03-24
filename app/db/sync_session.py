from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import settings


def _sync_database_url() -> str:
    u = settings.DATABASE_URL
    if "+asyncpg" in u:
        return u.replace("postgresql+asyncpg://", "postgresql://")
    return u


sync_engine = create_engine(_sync_database_url(), pool_pre_ping=True)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)
