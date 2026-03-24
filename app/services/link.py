from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import UUID4
from sqlalchemy import delete, func, select, update

from app.db.sync_session import SyncSessionLocal
from app.models import Link as LinkModel
from app.schemas.links import (
    LinkEntityType,
    LinkGetPaginatedResponse,
    LinkRequest,
    LinkResponse,
    LinkUpdateRequest,
)
from app.utils import calculate_time_ago
from app.utils.redis_cache import ProjectSummaryCache, cache_service


class LinkService:
    CACHE_TTL_LINKS = 180

    def __init__(self, user_timezone: str = "utc"):
        self.user_timezone = user_timezone

    def create_link(
        self,
        link_request: LinkRequest,
        entity_id: UUID4,
        entity_type: LinkEntityType,
    ) -> LinkResponse:
        db = SyncSessionLocal()
        try:
            row = LinkModel(
                title=link_request.title,
                link_url=str(link_request.link_url),
                entity_id=UUID(str(entity_id)),
                entity_type=entity_type.value,
                created_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create link: {e}",
            )
        finally:
            db.close()

        cache_service.invalidate_pattern(f"links:list:{entity_type}:{entity_id}:*")
        if entity_type == LinkEntityType.PROJECT:
            ProjectSummaryCache.delete_summary(str(entity_id))

        return LinkResponse(
            id=str(row.id),
            title=row.title,
            link_url=row.link_url,
            created_time=calculate_time_ago(row.created_at, self.user_timezone),
        )

    def get_links(
        self,
        entity_id: UUID4,
        entity_type: LinkEntityType,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> LinkGetPaginatedResponse:
        cache_key = f"links:list:{entity_type}:{entity_id}:{limit}:{offset}"
        cached = cache_service.get(cache_key)
        if cached:
            return LinkGetPaginatedResponse(**cached)

        from app.core import settings as app_settings

        db = SyncSessionLocal()
        try:
            filt = (LinkModel.entity_id == UUID(str(entity_id))) & (
                LinkModel.entity_type == entity_type.value
            )
            total = db.execute(select(func.count()).select_from(LinkModel).where(filt)).scalar() or 0

            stmt = select(LinkModel).where(filt).order_by(LinkModel.created_at.desc())
            off = offset if offset is not None else app_settings.DEFAULT_PAGINATION_OFFSET
            lim = limit if limit is not None else app_settings.DEFAULT_PAGINATION_LIMIT
            stmt = stmt.offset(off).limit(lim)
            rows = list(db.execute(stmt).scalars().all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get links: {e}",
            )
        finally:
            db.close()

        links = [
            LinkResponse(
                id=str(r.id),
                title=r.title,
                link_url=r.link_url,
                created_time=calculate_time_ago(r.created_at, self.user_timezone),
            )
            for r in rows
        ]
        result = LinkGetPaginatedResponse(
            links=links,
            total=total,
            offset=off,
            limit=lim,
        )
        cache_service.set(cache_key, result.model_dump(mode="json"), ttl=self.CACHE_TTL_LINKS)
        return result

    def update_link(self, link_id: UUID4, link_request: LinkUpdateRequest) -> LinkResponse:
        link_id_u = UUID(str(link_id))
        db = SyncSessionLocal()
        link_data = None
        try:
            row = db.execute(select(LinkModel).where(LinkModel.id == link_id_u)).scalar_one_or_none()
            if row:
                link_data = {"entity_id": str(row.entity_id), "entity_type": row.entity_type}

            updates = {}
            if link_request.title is not None:
                updates["title"] = link_request.title
            if link_request.link_url is not None:
                updates["link_url"] = str(link_request.link_url)
            if not updates:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No updates provided",
                )

            res = db.execute(update(LinkModel).where(LinkModel.id == link_id_u).values(**updates))
            db.commit()
            if res.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")
            row = db.execute(select(LinkModel).where(LinkModel.id == link_id_u)).scalar_one()
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update link: {e}",
            )
        finally:
            db.close()

        if link_data:
            cache_service.invalidate_pattern(
                f"links:list:{link_data['entity_type']}:{link_data['entity_id']}:*"
            )
            if link_data["entity_type"] == LinkEntityType.PROJECT.value:
                ProjectSummaryCache.delete_summary(link_data["entity_id"])

        return LinkResponse(
            id=str(row.id),
            title=row.title,
            link_url=row.link_url,
            created_time=calculate_time_ago(row.created_at, self.user_timezone),
        )

    def delete_link(self, link_id: UUID4) -> bool:
        link_id_u = UUID(str(link_id))
        db = SyncSessionLocal()
        link_data = None
        try:
            row = db.execute(select(LinkModel).where(LinkModel.id == link_id_u)).scalar_one_or_none()
            if row:
                link_data = {"entity_id": str(row.entity_id), "entity_type": row.entity_type}
            res = db.execute(delete(LinkModel).where(LinkModel.id == link_id_u))
            db.commit()
            if res.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete link: {e}",
            )
        finally:
            db.close()

        if link_data:
            cache_service.invalidate_pattern(
                f"links:list:{link_data['entity_type']}:{link_data['entity_id']}:*"
            )
            if link_data["entity_type"] == LinkEntityType.PROJECT.value:
                ProjectSummaryCache.delete_summary(link_data["entity_id"])
        return True

    def delete_all(self, entity_id: UUID4, entity_type: LinkEntityType) -> bool:
        db = SyncSessionLocal()
        try:
            db.execute(
                delete(LinkModel).where(
                    LinkModel.entity_id == UUID(str(entity_id)),
                    LinkModel.entity_type == entity_type.value,
                )
            )
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all links: {e}",
            )
        finally:
            db.close()

        cache_service.invalidate_pattern(f"links:list:{entity_type}:{entity_id}:*")
        if entity_type == LinkEntityType.PROJECT:
            ProjectSummaryCache.delete_summary(str(entity_id))
        return True
