import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import UUID4
from app.utils.uuid_compat import as_uuid
from sqlalchemy import delete, func, select, update

from app.db.sync_session import SyncSessionLocal
from app.models import Inbox as InboxRow
from app.models import Profile
from app.schemas.inbox import (
    InboxArchiveResponse,
    InboxDeleteResponse,
    InboxEventType,
    InboxGetPaginatedResponse,
    InboxGetResponse,
    InboxMarkReadResponse,
    InboxResponse,
    InboxUnarchiveResponse,
)
from app.utils import calculate_time_ago
from app.utils.redis_cache import cache_service

logger = logging.getLogger(__name__)


class InboxService:
    CACHE_TTL = 300

    def __init__(self):
        pass

    def get_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxGetResponse:
        cache_key = f"inbox:{inbox_id}"
        cached = cache_service.get(cache_key)
        if cached:
            return InboxGetResponse(**cached)

        db = SyncSessionLocal()
        try:
            row = db.execute(
                select(InboxRow).where(
                    InboxRow.id == UUID(str(inbox_id)),
                    InboxRow.user_id == UUID(str(user_id)),
                )
            ).scalar_one_or_none()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get inbox: {e}",
            )
        finally:
            db.close()

        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")

        user_time_zone = self._get_user_time_zone(user_id)
        message_time = calculate_time_ago(row.created_at, user_time_zone)
        result = InboxGetResponse(
            id=str(row.id),
            title=row.title,
            message=row.message,
            message_time=message_time,
            is_read=row.is_read,
            is_archived=row.is_archived,
            event_type=row.event_type,
            reference_id=str(row.reference_id) if row.reference_id else None,
        )
        cache_service.set(cache_key, result.model_dump(mode="json"), ttl=self.CACHE_TTL)
        return result

    def create_inbox(
        self,
        title: str,
        message: str,
        user_id: UUID4,
        org_id: UUID4,
        user_by: UUID4,
        event_type: Optional[InboxEventType] = None,
        reference_id: Optional[UUID4] = None,
    ) -> InboxResponse:
        if event_type and reference_id:
            db = SyncSessionLocal()
            try:
                one_minute_ago = datetime.now(timezone.utc) - timedelta(minutes=1)
                stmt = select(InboxRow.id).where(
                    InboxRow.user_id == UUID(str(user_id)),
                    InboxRow.org_id == UUID(str(org_id)),
                    InboxRow.event_type == event_type.value,
                    InboxRow.reference_id == UUID(str(reference_id)),
                    InboxRow.created_at >= one_minute_ago,
                )
                existing_id = db.execute(stmt).scalar_one_or_none()
                if existing_id:
                    logger.info(
                        "Duplicate inbox notification prevented for user %s, event_type %s, reference_id %s",
                        user_id,
                        event_type.value,
                        reference_id,
                    )
                    dup = db.execute(select(InboxRow).where(InboxRow.id == existing_id)).scalar_one()
                    utz = self._get_user_time_zone(user_id)
                    return InboxResponse(
                        id=str(dup.id),
                        title=dup.title,
                        message=dup.message,
                        message_time=calculate_time_ago(dup.created_at, utz),
                        is_read=dup.is_read,
                        is_archived=dup.is_archived,
                        event_type=dup.event_type,
                        reference_id=str(dup.reference_id) if dup.reference_id else None,
                    )
            except Exception as e:
                logger.warning("Failed to check for duplicate inbox: %s", e)
            finally:
                db.close()

        db = SyncSessionLocal()
        try:
            row = InboxRow(
                title=title,
                message=message,
                user_id=UUID(str(user_id)),
                org_id=UUID(str(org_id)),
                user_by=UUID(str(user_by)),
                is_read=False,
                is_archived=False,
                event_type=event_type.value if event_type else None,
                reference_id=UUID(str(reference_id)) if reference_id else None,
                created_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        except Exception as e:
            db.rollback()
            logger.error("Failed to create inbox: %s", e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create inbox: {e}",
            )
        finally:
            db.close()

        user_time_zone = self._get_user_time_zone(user_id)
        message_time = calculate_time_ago(row.created_at, user_time_zone)
        self._invalidate_user_inbox_cache(user_id, org_id)
        return InboxResponse(
            id=str(row.id),
            title=row.title,
            message=row.message,
            message_time=message_time,
            is_read=row.is_read,
            is_archived=row.is_archived,
            event_type=row.event_type,
            reference_id=str(row.reference_id) if row.reference_id else None,
        )

    def get_all_inbox(
        self,
        user_id: UUID4,
        org_id: UUID4,
        include_archived: bool = False,
        unread_only: bool = False,
        order_by: Optional[str] = "desc",
        limit: Optional[int] = 50,
        offset: Optional[int] = 0,
    ) -> InboxGetPaginatedResponse:
        if order_by not in ("asc", "desc"):
            order_by = "desc"

        cache_key = f"inbox:list:{user_id}:{org_id}:{include_archived}:{unread_only}:{order_by}:{limit}:{offset}"
        cached = cache_service.get(cache_key)
        if cached:
            return InboxGetPaginatedResponse(**cached)

        db = SyncSessionLocal()
        try:
            filt: Any = (InboxRow.user_id == UUID(str(user_id))) & (
                InboxRow.org_id == UUID(str(org_id))
            )
            if not include_archived:
                filt = filt & (InboxRow.is_archived.is_(False))
            if unread_only:
                filt = filt & (InboxRow.is_read.is_(False))

            total = db.execute(
                select(func.count()).select_from(InboxRow).where(filt)
            ).scalar() or 0

            stmt = select(InboxRow).where(filt)
            if order_by == "asc":
                stmt = stmt.order_by(InboxRow.created_at.asc())
            else:
                stmt = stmt.order_by(InboxRow.created_at.desc())
            off = offset or 0
            lim = limit or 50
            stmt = stmt.offset(off).limit(lim)
            rows = list(db.execute(stmt).scalars().all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get all inbox: {e}",
            )
        finally:
            db.close()

        user_time_zone = self._get_user_time_zone(user_id)
        inboxes = [
            InboxResponse(
                id=str(r.id),
                title=r.title,
                message=r.message,
                message_time=calculate_time_ago(r.created_at, user_time_zone),
                is_read=r.is_read,
                is_archived=r.is_archived,
                event_type=r.event_type,
                reference_id=str(r.reference_id) if r.reference_id else None,
            )
            for r in rows
        ]
        result = InboxGetPaginatedResponse(
            inbox=inboxes,
            total=total,
            offset=off,
            limit=lim,
        )
        cache_service.set(cache_key, result.model_dump(mode="json"), ttl=self.CACHE_TTL)
        return result

    def get_archived_inbox(
        self,
        user_id: UUID4,
        org_id: UUID4,
        limit: Optional[int] = 50,
        offset: Optional[int] = 0,
    ) -> InboxGetPaginatedResponse:
        cache_key = f"inbox:archived:{user_id}:{org_id}:{limit}:{offset}"
        cached = cache_service.get(cache_key)
        if cached:
            return InboxGetPaginatedResponse(**cached)

        db = SyncSessionLocal()
        try:
            filt = (
                (InboxRow.user_id == UUID(str(user_id)))
                & (InboxRow.org_id == UUID(str(org_id)))
                & (InboxRow.is_archived.is_(True))
            )
            total = db.execute(
                select(func.count()).select_from(InboxRow).where(filt)
            ).scalar() or 0
            off = offset or 0
            lim = limit or 50
            rows = list(
                db.execute(
                    select(InboxRow)
                    .where(filt)
                    .order_by(InboxRow.created_at.desc())
                    .offset(off)
                    .limit(lim)
                )
                .scalars()
                .all()
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get archived inbox: {e}",
            )
        finally:
            db.close()

        user_time_zone = self._get_user_time_zone(user_id)
        inboxes = [
            InboxResponse(
                id=str(r.id),
                title=r.title,
                message=r.message,
                message_time=calculate_time_ago(r.created_at, user_time_zone),
                is_read=r.is_read,
                is_archived=r.is_archived,
                event_type=r.event_type,
                reference_id=str(r.reference_id) if r.reference_id else None,
            )
            for r in rows
        ]
        result = InboxGetPaginatedResponse(
            inbox=inboxes,
            total=total,
            offset=off,
            limit=lim,
        )
        cache_service.set(cache_key, result.model_dump(mode="json"), ttl=self.CACHE_TTL)
        return result

    def mark_read(self, inbox_id: UUID4, user_id: UUID4) -> InboxMarkReadResponse:
        db = SyncSessionLocal()
        org_id: Optional[str] = None
        try:
            inbox = db.execute(
                select(InboxRow).where(
                    InboxRow.id == UUID(str(inbox_id)),
                    InboxRow.user_id == UUID(str(user_id)),
                )
            ).scalar_one_or_none()
            if not inbox:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")
            org_id = str(inbox.org_id)
            db.execute(
                update(InboxRow)
                .where(
                    InboxRow.id == UUID(str(inbox_id)),
                    InboxRow.user_id == UUID(str(user_id)),
                )
                .values(
                    is_read=True,
                    read_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to mark inbox as read: {e}",
            )
        finally:
            db.close()

        if org_id:
            self._invalidate_inbox_cache(inbox_id, user_id, org_id)
        return InboxMarkReadResponse(success=True, message="Inbox marked as read")

    def archive_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxArchiveResponse:
        db = SyncSessionLocal()
        try:
            res = db.execute(
                update(InboxRow)
                .where(
                    InboxRow.id == UUID(str(inbox_id)),
                    InboxRow.user_id == UUID(str(user_id)),
                )
                .values(
                    is_archived=True,
                    archived_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
            if res.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")
            org_id = db.execute(
                select(InboxRow.org_id).where(InboxRow.id == UUID(str(inbox_id)))
            ).scalar_one_or_none()
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to archive inbox: {e}",
            )
        finally:
            db.close()

        self._invalidate_inbox_cache(inbox_id, user_id, str(org_id) if org_id else None)
        return InboxArchiveResponse(success=True, message="Inbox archived successfully")

    def unarchive_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxUnarchiveResponse:
        db = SyncSessionLocal()
        try:
            res = db.execute(
                update(InboxRow)
                .where(
                    InboxRow.id == UUID(str(inbox_id)),
                    InboxRow.user_id == UUID(str(user_id)),
                )
                .values(is_archived=False, archived_at=None)
            )
            db.commit()
            if res.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")
            org_id = db.execute(
                select(InboxRow.org_id).where(InboxRow.id == UUID(str(inbox_id)))
            ).scalar_one_or_none()
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to unarchive inbox: {e}",
            )
        finally:
            db.close()

        self._invalidate_inbox_cache(inbox_id, user_id, str(org_id) if org_id else None)
        return InboxUnarchiveResponse(success=True, message="Inbox restored successfully")

    def delete_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxDeleteResponse:
        db = SyncSessionLocal()
        try:
            org_id = db.execute(
                select(InboxRow.org_id).where(
                    InboxRow.id == UUID(str(inbox_id)),
                    InboxRow.user_id == UUID(str(user_id)),
                )
            ).scalar_one_or_none()
            if not org_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")
            res = db.execute(
                delete(InboxRow).where(
                    InboxRow.id == UUID(str(inbox_id)),
                    InboxRow.user_id == UUID(str(user_id)),
                )
            )
            db.commit()
            if res.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox not found")
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete inbox: {e}",
            )
        finally:
            db.close()

        self._invalidate_inbox_cache(inbox_id, user_id, str(org_id))
        return InboxDeleteResponse(success=True, message="Inbox deleted successfully")

    def get_unread_count(self, user_id: UUID4, org_id: UUID4) -> int:
        cache_key = f"inbox:unread:{user_id}:{org_id}"
        cached = cache_service.get(cache_key)
        if cached is not None:
            return int(cached)

        db = SyncSessionLocal()
        try:
            count = db.execute(
                select(func.count())
                .select_from(InboxRow)
                .where(
                    InboxRow.user_id == UUID(str(user_id)),
                    InboxRow.org_id == UUID(str(org_id)),
                    InboxRow.is_read.is_(False),
                    InboxRow.is_archived.is_(False),
                )
            ).scalar() or 0
        except Exception as e:
            logger.error("Failed to get unread count: %s", e)
            return 0
        finally:
            db.close()

        cache_service.set(cache_key, count, ttl=60)
        return count

    def _get_user_time_zone(self, user_id: UUID4) -> str:
        cache_key = f"user:timezone:{user_id}"
        cached = cache_service.get(cache_key)
        if cached:
            return cached

        db = SyncSessionLocal()
        try:
            tz = db.execute(
                select(Profile.timezone).where(Profile.user_id == UUID(str(user_id)))
            ).scalar_one_or_none()
        except Exception as e:
            logger.error("Failed to get user timezone: %s", e)
            return "UTC"
        finally:
            db.close()

        timezone_val = tz or "UTC"
        cache_service.set(cache_key, timezone_val, ttl=3600)
        return timezone_val

    def _invalidate_inbox_cache(
        self, inbox_id: UUID4, user_id: UUID4, org_id: Optional[str] = None
    ):
        try:
            cache_service.delete(f"inbox:{inbox_id}")
            if org_id:
                self._invalidate_user_inbox_cache(user_id, as_uuid(org_id))
        except Exception as e:
            logger.warning("Redis delete error: %s", e)

    def _invalidate_user_inbox_cache(self, user_id: UUID4, org_id: UUID4):
        try:
            cache_service.invalidate_pattern(f"inbox:list:{user_id}:{org_id}:*")
            cache_service.invalidate_pattern(f"inbox:archived:{user_id}:{org_id}:*")
            cache_service.delete(f"inbox:unread:{user_id}:{org_id}")
        except Exception as e:
            logger.warning("Redis delete error: %s", e)
