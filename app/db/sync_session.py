from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.sync_url import get_sync_database_url

sync_engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)
