from contextlib import contextmanager
from typing import Generator

from sqlalchemy.orm import Session

from app.db.sync_session import SyncSessionLocal


@contextmanager
def sync_session() -> Generator[Session, None, None]:
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
