import logging
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import UUID4
from sqlalchemy import and_, delete, func, select

from app.db.sync_session import SyncSessionLocal
from app.models import Activity, Profile, Task
from app.schemas.activities import (
    ActivityGetPaginatedResponse,
    ActivityResponse,
    ActivityType,
)
from app.services.files import FilesService
from app.utils import calculate_time_ago
from app.utils.redis_cache import ProjectSummaryCache, cache_service
from app.utils.sa_pagination import apply_sa_limit_offset

logger = logging.getLogger(__name__)


def _profile_join_stmt(base_filter):
    return (
        select(Activity, Profile.display_name, Profile.avatar_file_id, Profile.timezone)
        .join(Profile, Profile.id == Activity.actor_profile_id)
        .where(base_filter)
        .order_by(Activity.created_at.desc())
    )


def _rows_to_activity_responses(
    rows: List[Any],
    files_service: FilesService,
) -> List[ActivityResponse]:
    out: List[ActivityResponse] = []
    for activity, display_name, avatar_file_id, tz in rows:
        avatar_url = None
        if avatar_file_id:
            try:
                avatar_url = files_service.get_file_url(avatar_file_id)
            except Exception:
                pass
        tz_s = (tz or "utc").lower()
        out.append(
            ActivityResponse(
                id=activity.id,
                user_display_name=display_name or "Unknown",
                user_avatar_url=avatar_url,
                description=activity.description or "",
                activity_time=calculate_time_ago(activity.created_at, tz_s),
            )
        )
    return out


def _merged_dicts_from_rows(rows):
    merged = []
    for activity, display_name, avatar_file_id, tz in rows:
        merged.append(
            {
                "id": activity.id,
                "description": activity.description,
                "created_at": activity.created_at,
                "activity_type": activity.activity_type,
                "profiles": {
                    "display_name": display_name,
                    "avatar_file_id": str(avatar_file_id) if avatar_file_id else None,
                    "timezone": tz or "utc",
                },
            }
        )
    return merged


class ActivityService:
    CACHE_TTL_ACTIVITIES = 120

    def __init__(self, files_service: FilesService):
        self.files_service = files_service

    def add_activity(
        self,
        activity_type: ActivityType,
        entity_id: UUID4,
        actor_id: UUID4,
        description: str,
    ):
        try:
            db = SyncSessionLocal()
            try:
                profile_id = db.execute(
                    select(Profile.id).where(Profile.user_id == UUID(str(actor_id)))
                ).scalar_one_or_none()
                if not profile_id:
                    logger.warning(
                        "Profile not found for user_id %s, skipping activity insertion", actor_id
                    )
                    return
                act = Activity(
                    entity_id=UUID(str(entity_id)),
                    actor_profile_id=profile_id,
                    activity_type=activity_type.value,
                    description=description,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(act)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error("Failed to add activity: %s", e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add activity: {e!s}",
            )

        cache_service.invalidate_pattern(f"activities:list:{entity_id}:*")
        cache_service.invalidate_pattern(f"activities:paginated:{entity_id}:*")

        if activity_type == ActivityType.PROJECT:
            ProjectSummaryCache.delete_summary(str(entity_id))
        elif activity_type == ActivityType.TASK:
            try:
                db = SyncSessionLocal()
                try:
                    pid = db.execute(
                        select(Task.project_id).where(Task.id == UUID(str(entity_id)))
                    ).scalar_one_or_none()
                finally:
                    db.close()
                if pid:
                    ProjectSummaryCache.delete_summary(str(pid))
                    cache_service.invalidate_pattern(f"activities:list:{pid}:*")
                    cache_service.invalidate_pattern(f"activities:paginated:{pid}:*")
            except Exception as e:
                logger.warning("Failed to get project_id for task activity: %s", e)

    def get_activities(
        self,
        entity_id: UUID4,
        activity_type: ActivityType,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[ActivityResponse]:
        cache_key = f"activities:list:{entity_id}:{activity_type}:{limit}:{offset}"
        cached = cache_service.get(cache_key)
        if cached:
            return [ActivityResponse(**item) for item in cached]

        eid = UUID(str(entity_id))

        if activity_type == ActivityType.TASK:
            base = and_(Activity.entity_id == eid, Activity.activity_type == "task")
            try:
                db = SyncSessionLocal()
                try:
                    stmt = _profile_join_stmt(base)
                    _, off, stmt = apply_sa_limit_offset(stmt, limit, offset)
                    rows = db.execute(stmt).all()
                finally:
                    db.close()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to get activities: {e!s}",
                )
            activities = _rows_to_activity_responses(rows, self.files_service)
            cache_service.set(
                cache_key,
                [a.model_dump(mode="json") for a in activities],
                ttl=self.CACHE_TTL_ACTIVITIES,
            )
            return activities

        try:
            db = SyncSessionLocal()
            try:
                task_ids = list(
                    db.execute(select(Task.id).where(Task.project_id == eid)).scalars().all()
                )
                all_rows = []
                all_rows.extend(
                    db.execute(
                        _profile_join_stmt(
                            and_(Activity.entity_id == eid, Activity.activity_type == "project")
                        )
                    ).all()
                )
                if task_ids:
                    all_rows.extend(
                        db.execute(
                            _profile_join_stmt(
                                and_(
                                    Activity.activity_type == "task",
                                    Activity.entity_id.in_(task_ids),
                                )
                            )
                        ).all()
                    )
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get activities: {e!s}",
            )

        merged = _merged_dicts_from_rows(all_rows)
        merged.sort(key=lambda x: x["created_at"], reverse=True)

        if offset is not None and limit is not None:
            merged = merged[offset : offset + limit]
        elif offset is not None:
            merged = merged[offset:]
        elif limit is not None:
            merged = merged[:limit]

        activities = []
        for activity in merged:
            avatar_url = None
            profile = activity.get("profiles", {})
            if profile and profile.get("avatar_file_id"):
                try:
                    avatar_url = self.files_service.get_file_url(
                        UUID(profile["avatar_file_id"])
                    )
                except Exception:
                    pass
            activities.append(
                ActivityResponse(
                    id=activity["id"],
                    user_display_name=profile.get("display_name", "Unknown") if profile else "Unknown",
                    user_avatar_url=avatar_url,
                    description=activity["description"],
                    activity_time=calculate_time_ago(
                        activity["created_at"],
                        profile.get("timezone", "utc") if profile else "utc",
                    ),
                )
            )

        cache_service.set(
            cache_key,
            [a.model_dump(mode="json") for a in activities],
            ttl=self.CACHE_TTL_ACTIVITIES,
        )
        return activities

    def get_activities_paginated(
        self,
        entity_id: UUID4,
        activity_type: ActivityType,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ActivityGetPaginatedResponse:
        cache_key = f"activities:paginated:{entity_id}:{activity_type}:{limit}:{offset}"
        cached = cache_service.get(cache_key)
        if cached:
            return ActivityGetPaginatedResponse(**cached)

        eid = UUID(str(entity_id))

        if activity_type == ActivityType.TASK:
            base = and_(Activity.entity_id == eid, Activity.activity_type == "task")
            try:
                db = SyncSessionLocal()
                try:
                    total_count = int(
                        db.execute(
                            select(func.count()).select_from(Activity).where(base)
                        ).scalar_one()
                    )
                    stmt = _profile_join_stmt(base)
                    lim, off, stmt = apply_sa_limit_offset(stmt, limit, offset)
                    rows = db.execute(stmt).all()
                finally:
                    db.close()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to get activities: {e!s}",
                )
            activities = []
            for activity, display_name, avatar_file_id, tz in rows:
                avatar_url = None
                if avatar_file_id:
                    try:
                        avatar_url = self.files_service.get_file_url(avatar_file_id)
                    except Exception:
                        pass
                activities.append(
                    ActivityResponse(
                        id=activity.id,
                        user_display_name=display_name or "",
                        user_avatar_url=avatar_url,
                        description=activity.description or "",
                        activity_time=calculate_time_ago(
                            activity.created_at, (tz or "utc").lower()
                        ),
                    )
                )
            result = ActivityGetPaginatedResponse(
                activities=activities,
                total=total_count,
                offset=off,
                limit=lim,
            )
            cache_service.set(cache_key, result.model_dump(mode="json"), ttl=self.CACHE_TTL_ACTIVITIES)
            return result

        try:
            db = SyncSessionLocal()
            try:
                task_ids = list(
                    db.execute(select(Task.id).where(Task.project_id == eid)).scalars().all()
                )
                all_rows = []
                all_rows.extend(
                    db.execute(
                        _profile_join_stmt(
                            and_(Activity.entity_id == eid, Activity.activity_type == "project")
                        )
                    ).all()
                )
                if task_ids:
                    all_rows.extend(
                        db.execute(
                            _profile_join_stmt(
                                and_(
                                    Activity.activity_type == "task",
                                    Activity.entity_id.in_(task_ids),
                                )
                            )
                        ).all()
                    )
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get activities: {e!s}",
            )

        merged = _merged_dicts_from_rows(all_rows)
        merged.sort(key=lambda x: x["created_at"], reverse=True)
        total_count = len(merged)

        if offset is not None and limit is not None:
            merged = merged[offset : offset + limit]
        elif offset is not None:
            merged = merged[offset:]
        elif limit is not None:
            merged = merged[:limit]

        activities = []
        for activity in merged:
            avatar_url = None
            profile = activity.get("profiles", {})
            if profile and profile.get("avatar_file_id"):
                try:
                    avatar_url = self.files_service.get_file_url(
                        UUID(profile["avatar_file_id"])
                    )
                except Exception:
                    pass
            activities.append(
                ActivityResponse(
                    id=activity["id"],
                    user_display_name=profile.get("display_name", "") if profile else "",
                    user_avatar_url=avatar_url,
                    description=activity["description"],
                    activity_time=calculate_time_ago(
                        activity["created_at"],
                        profile.get("timezone", "utc") if profile else "utc",
                    ),
                )
            )

        result = ActivityGetPaginatedResponse(
            activities=activities,
            total=total_count,
            offset=offset,
            limit=limit,
        )
        cache_service.set(cache_key, result.model_dump(mode="json"), ttl=self.CACHE_TTL_ACTIVITIES)
        return result

    def delete_activity(self, activity_id: UUID4) -> bool:
        entity_id = None
        try:
            db = SyncSessionLocal()
            try:
                row = db.execute(
                    select(Activity.entity_id).where(Activity.id == UUID(str(activity_id)))
                ).first()
                if row:
                    entity_id = row[0]
                db.execute(delete(Activity).where(Activity.id == UUID(str(activity_id))))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete activity: {e!s}",
            )

        if entity_id:
            cache_service.invalidate_pattern(f"activities:list:{entity_id}:*")
            cache_service.invalidate_pattern(f"activities:paginated:{entity_id}:*")
        return True

    def delete_all(self, entity_id: UUID4, activity_type: ActivityType) -> bool:
        eid = UUID(str(entity_id))
        try:
            db = SyncSessionLocal()
            try:
                db.execute(
                    delete(Activity).where(
                        Activity.entity_id == eid,
                        Activity.activity_type == activity_type.value,
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all activities: {e!s}",
            )

        cache_service.invalidate_pattern(f"activities:list:{entity_id}:*")
        cache_service.invalidate_pattern(f"activities:paginated:{entity_id}:*")
        return True
