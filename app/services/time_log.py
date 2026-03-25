from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from pydantic import UUID4
from sqlalchemy import and_, delete, func, select

from app.db.sync_session import SyncSessionLocal
from app.models import Profile, Project, Task, TimeLog
from app.schemas.time_logs.time_logs import (
    TimeLogStartRequest,
    TimeLogStopRequest,
    TimeLogCreateRequest,
    TimeLogStartResponse,
    TimeLogStopResponse,
    TimeLogCreateResponse,
    TimeLogGetResponse,
    TimeLogListResponse,
    TimeLogUpdateRequest,
    TimeLogUpdateResponse,
    TimeLogDeleteResponse,
    TimeLogStatus,
)
from app.utils.redis_cache import cache_service


def format_duration(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_time_string(time_str: str) -> time:
    """Parse time string from database (time-only or full ISO timestamp)."""
    time_str = time_str.strip()
    if "T" in time_str or (len(time_str) > 12 and ("+" in time_str or "-" in time_str[-6:])):
        return datetime.fromisoformat(time_str.replace("Z", "+00:00")).time()
    if "." in time_str:
        return datetime.strptime(time_str, "%H:%M:%S.%f").time()
    return datetime.strptime(time_str, "%H:%M:%S").time()


def _parse_stored_to_utc_datetime(stored_value, fallback_date: Optional[date] = None) -> Optional[datetime]:
    """Parse stored started_at/stoped_at to UTC datetime. Uses timestamp date when full ISO; fallback_date for time-only."""
    if stored_value is None:
        return None
    if isinstance(stored_value, datetime):
        dt = stored_value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    s = stored_value.strip() if isinstance(stored_value, str) else str(stored_value)
    if "T" in s or (len(s) > 12 and ("+" in s or s.endswith("Z") or "-" in s[-6:])):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    t = parse_time_string(s)
    d = fallback_date or date.today()
    return datetime.combine(d, t, tzinfo=timezone.utc)


class TimeLogService:
    CACHE_TTL_ACTIVE = 30  # 30 seconds for active time log (real-time)
    CACHE_TTL_LIST = 180  # 3 minutes for lists
    CACHE_TTL_SINGLE = 300  # 5 minutes for single time log
    
    def __init__(self):
        pass
    
    def _get_user_timezone(self, user_id: UUID4) -> str:
        """Get user timezone with caching (one lookup per request)."""
        cache_key = f"user:timezone:{user_id}"
        cached = cache_service.get(cache_key)
        if cached and isinstance(cached, str) and cached.strip():
            return cached.strip()
        
        db = SyncSessionLocal()
        try:
            timezone_val = db.execute(
                select(Profile.timezone).where(Profile.user_id == UUID(str(user_id)))
            ).scalar_one_or_none()
        except Exception:
            return "UTC"
        finally:
            db.close()

        if not timezone_val:
            return "UTC"
        timezone_val = timezone_val or "UTC"
        if not isinstance(timezone_val, str) or not timezone_val.strip():
            timezone_val = 'UTC'
        else:
            timezone_val = timezone_val.strip()
        
        cache_service.set(cache_key, timezone_val, ttl=3600)
        
        return timezone_val
    
    def _convert_time_to_user_tz(self, time_obj: time, date_obj: date, user_timezone: str) -> time:
        """Convert UTC time to user timezone"""
        try:
            utc_dt = datetime.combine(date_obj, time_obj, tzinfo=timezone.utc)
            user_tz = ZoneInfo(user_timezone)
            user_dt = utc_dt.astimezone(user_tz)
            return user_dt.time()
        except Exception:
            return time_obj
    
    def _convert_time_to_utc(self, time_obj: time, date_obj: date, user_timezone: str) -> time:
        """Convert user timezone time to UTC"""
        try:
            user_tz = ZoneInfo(user_timezone)
            user_dt = datetime.combine(date_obj, time_obj, tzinfo=user_tz)
            utc_dt = user_dt.astimezone(timezone.utc)
            return utc_dt.time()
        except Exception:
            return time_obj

    def _stored_timestamp_to_user_time(self, stored_value, user_timezone: str, fallback_date: Optional[date] = None) -> Optional[time]:
        """Convert stored started_at/stoped_at to user TZ. Uses timestamp date when full ISO for correct cross-midnight."""
        utc_dt = _parse_stored_to_utc_datetime(stored_value, fallback_date)
        if utc_dt is None:
            return None
        try:
            return utc_dt.astimezone(ZoneInfo(user_timezone)).time()
        except Exception:
            return utc_dt.time() if fallback_date else None
    
    def _invalidate_time_log_caches(self, user_id: UUID4, time_log_id: Optional[UUID4] = None, organization_id: Optional[UUID4] = None):
        """Invalidate time log caches"""
        if organization_id:
            cache_service.delete(f"time_log:active:{user_id}:{organization_id}")
            cache_service.invalidate_pattern(f"time_logs:list:{organization_id}:*")
        else:
            # Fallback: invalidate all active time logs for user (less efficient but safe)
            cache_service.invalidate_pattern(f"time_log:active:{user_id}:*")
            cache_service.invalidate_pattern(f"time_logs:list:*")
        if time_log_id:
            cache_service.delete(f"time_log:{time_log_id}")
    
    def invalidate_user_timezone_caches(self, user_id: UUID4):
        """Invalidate user timezone cache and all related time log caches
        
        This should be called when a user changes their timezone setting,
        as all cached time logs will have incorrect time values.
        """
        cache_service.delete(f"user:timezone:{user_id}")
        
        cache_service.invalidate_pattern(f"time_log:active:{user_id}:*")
        
        cache_service.invalidate_pattern(f"time_logs:list:*:{user_id}:*")
        
        cache_service.invalidate_pattern(f"time_log:*")
    
    def _verify_time_log_organization(self, time_log_id: UUID4, organization_id: UUID4) -> None:
        """Verify that a time log's project belongs to the organization"""
        try:
            db = SyncSessionLocal()
            try:
                project_id = db.execute(
                    select(TimeLog.project_id).where(TimeLog.id == UUID(str(time_log_id)))
                ).scalar_one_or_none()
                if not project_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Time log not found",
                    )
                org_id = db.execute(
                    select(Project.org_id).where(Project.id == project_id)
                ).scalar_one_or_none()
                if not org_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Project not found",
                    )
                if str(org_id) != str(organization_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Time log does not belong to this organization",
                    )
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify time log organization: {str(e)}",
            )
    
    def start_time_log(
        self,
        time_log_request: TimeLogStartRequest,
        user_id: UUID4,
        organization_id: UUID4,
    ) -> TimeLogStartResponse:
        row: Optional[TimeLog] = None
        try:
            db = SyncSessionLocal()
            try:
                project_ids = list(
                    db.execute(
                        select(Project.id).where(Project.org_id == UUID(str(organization_id)))
                    ).scalars().all()
                )
                if not project_ids:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="No projects found in organization",
                    )
                has_active = (
                    db.execute(
                        select(TimeLog.id)
                        .where(
                            TimeLog.created_by == UUID(str(user_id)),
                            TimeLog.status == TimeLogStatus.RUNNING.value,
                            TimeLog.project_id.in_(project_ids),
                        )
                        .limit(1)
                    ).first()
                    is not None
                )
                if has_active:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="You already have an active time log running. Please stop it before starting a new one.",
                    )
                proj = db.execute(
                    select(Project).where(Project.id == UUID(str(time_log_request.project_id)))
                ).scalar_one_or_none()
                if not proj:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Project not found",
                    )
                if str(proj.org_id) != str(organization_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Project does not belong to this organization",
                    )
                task = db.execute(
                    select(Task).where(Task.id == UUID(str(time_log_request.task_id)))
                ).scalar_one_or_none()
                if not task:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Task not found",
                    )
                if str(task.project_id) != str(time_log_request.project_id):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Task does not belong to the specified project",
                    )
                now = datetime.now(timezone.utc)
                current_date = now.date()
                row = TimeLog(
                    project_id=UUID(str(time_log_request.project_id)),
                    task_id=UUID(str(time_log_request.task_id)),
                    started_at=now,
                    date=current_date,
                    duration_seconds=0,
                    status=TimeLogStatus.RUNNING.value,
                    notes=time_log_request.notes,
                    created_by=UUID(str(user_id)),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to start time log: {str(e)}",
            )

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to start time log",
            )

        duration_seconds = row.duration_seconds
        user_tz = self._get_user_timezone(user_id)
        log_date = row.date if row.date else datetime.now(timezone.utc).date()
        started_at_user_tz = self._stored_timestamp_to_user_time(row.started_at, user_tz, log_date)

        result = TimeLogStartResponse(
            id=row.id,
            project_id=row.project_id,
            task_id=row.task_id,
            started_at=started_at_user_tz,
            stoped_at=None,
            date=log_date,
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=row.status,
            notes=row.notes,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

        self._invalidate_time_log_caches(user_id, organization_id=organization_id)

        return result
    
    def create_time_log(
        self,
        time_log_request: TimeLogCreateRequest,
        user_id: UUID4,
        organization_id: UUID4,
    ) -> TimeLogCreateResponse:
        row: Optional[TimeLog] = None
        user_tz = self._get_user_timezone(user_id)
        try:
            db = SyncSessionLocal()
            try:
                proj = db.execute(
                    select(Project).where(Project.id == UUID(str(time_log_request.project_id)))
                ).scalar_one_or_none()
                if not proj:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Project not found",
                    )
                if str(proj.org_id) != str(organization_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Project does not belong to this organization",
                    )
                task = db.execute(
                    select(Task).where(Task.id == UUID(str(time_log_request.task_id)))
                ).scalar_one_or_none()
                if not task:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Task not found",
                    )
                if str(task.project_id) != str(time_log_request.project_id):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Task does not belong to the specified project",
                    )

                started_at = self._convert_time_to_utc(
                    time_log_request.started_at, time_log_request.date, user_tz
                )
                stoped_at = (
                    self._convert_time_to_utc(
                        time_log_request.stoped_at, time_log_request.date, user_tz
                    )
                    if time_log_request.stoped_at
                    else None
                )
                duration_seconds = time_log_request.duration_seconds

                if stoped_at is None and duration_seconds is not None:
                    started_datetime = datetime.combine(time_log_request.date, started_at)
                    stopped_datetime = started_datetime + timedelta(seconds=duration_seconds)
                    stoped_at = stopped_datetime.time()
                elif stoped_at is not None and duration_seconds is None:
                    started_datetime = datetime.combine(time_log_request.date, started_at)
                    stopped_datetime = datetime.combine(time_log_request.date, stoped_at)
                    if stopped_datetime < started_datetime:
                        stopped_datetime = datetime.combine(time_log_request.date, time(23, 59, 59))
                    duration_seconds = int(round((stopped_datetime - started_datetime).total_seconds()))
                elif stoped_at is not None and duration_seconds is not None:
                    started_datetime = datetime.combine(time_log_request.date, started_at)
                    stopped_datetime = datetime.combine(time_log_request.date, stoped_at)
                    if stopped_datetime < started_datetime:
                        stopped_datetime = datetime.combine(time_log_request.date, time(23, 59, 59))
                    calculated_duration = (stopped_datetime - started_datetime).total_seconds()
                    duration_seconds = int(round(calculated_duration))

                duration_seconds = int(duration_seconds) if not isinstance(duration_seconds, int) else duration_seconds
                now = datetime.now(timezone.utc)
                started_at_dt = datetime.combine(time_log_request.date, started_at, tzinfo=timezone.utc)
                stoped_at_dt = (
                    datetime.combine(time_log_request.date, stoped_at, tzinfo=timezone.utc)
                    if stoped_at
                    else None
                )
                row = TimeLog(
                    project_id=UUID(str(time_log_request.project_id)),
                    task_id=UUID(str(time_log_request.task_id)),
                    started_at=started_at_dt,
                    stoped_at=stoped_at_dt,
                    date=time_log_request.date,
                    duration_seconds=duration_seconds,
                    status=TimeLogStatus.STOPPED.value,
                    notes=time_log_request.notes,
                    created_by=UUID(str(user_id)),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create time log: {str(e)}",
            )

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create time log",
            )

        duration_seconds = row.duration_seconds
        log_date = row.date if row.date else time_log_request.date
        started_at_user_tz = self._stored_timestamp_to_user_time(row.started_at, user_tz, log_date)
        stoped_at_user_tz = self._stored_timestamp_to_user_time(row.stoped_at, user_tz, log_date)

        result = TimeLogCreateResponse(
            id=row.id,
            project_id=row.project_id,
            task_id=row.task_id,
            started_at=started_at_user_tz,
            stoped_at=stoped_at_user_tz,
            date=log_date,
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=row.status,
            notes=row.notes,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

        self._invalidate_time_log_caches(user_id, organization_id=organization_id)

        return result
    
    def stop_time_log(
        self,
        time_log_id: UUID4,
        stop_request: TimeLogStopRequest,
        user_id: UUID4,
        organization_id: UUID4,
    ) -> TimeLogStopResponse:
        self._verify_time_log_organization(time_log_id, organization_id)

        updated: Optional[TimeLog] = None
        try:
            db = SyncSessionLocal()
            try:
                log = db.execute(
                    select(TimeLog).where(TimeLog.id == UUID(str(time_log_id)))
                ).scalar_one_or_none()
                if not log:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Time log not found",
                    )
                if str(log.created_by) != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You don't have permission to stop this time log",
                    )
                if log.status != TimeLogStatus.RUNNING.value:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Time log is not running",
                    )

                now = datetime.now(timezone.utc)
                log_date = log.date if log.date else now.date()
                sa_src = log.started_at
                if isinstance(sa_src, datetime):
                    started_datetime = (
                        sa_src.astimezone(timezone.utc)
                        if sa_src.tzinfo
                        else sa_src.replace(tzinfo=timezone.utc)
                    )
                elif isinstance(sa_src, str):
                    started_time = parse_time_string(sa_src)
                    started_datetime = datetime.combine(log_date, started_time, tzinfo=timezone.utc)
                else:
                    started_time = sa_src
                    started_datetime = datetime.combine(log_date, started_time, tzinfo=timezone.utc)

                stopped_datetime = now
                if stopped_datetime < started_datetime:
                    stopped_datetime = datetime.combine(log_date, time(23, 59, 59), tzinfo=timezone.utc)

                duration_seconds = int(round((stopped_datetime - started_datetime).total_seconds()))
                log.stoped_at = now
                log.duration_seconds = duration_seconds
                log.status = TimeLogStatus.STOPPED.value
                log.updated_at = now
                if stop_request.notes is not None:
                    log.notes = stop_request.notes
                db.commit()
                db.refresh(log)
                updated = log
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to stop time log: {str(e)}",
            )

        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time log not found",
            )

        duration_seconds = updated.duration_seconds
        user_tz = self._get_user_timezone(user_id)
        log_date = updated.date if updated.date else datetime.now(timezone.utc).date()
        started_at_user_tz = self._stored_timestamp_to_user_time(updated.started_at, user_tz, log_date)
        stoped_at_user_tz = self._stored_timestamp_to_user_time(updated.stoped_at, user_tz, log_date)

        result = TimeLogStopResponse(
            id=updated.id,
            project_id=updated.project_id,
            task_id=updated.task_id,
            started_at=started_at_user_tz,
            stoped_at=stoped_at_user_tz,
            date=log_date,
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=updated.status,
            notes=updated.notes,
            created_by=updated.created_by,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
        )

        self._invalidate_time_log_caches(user_id, time_log_id, organization_id=organization_id)

        return result
    
    def get_active_time_log(self, user_id: UUID4, organization_id: UUID4) -> Optional[TimeLogGetResponse]:
        cache_key = f"time_log:active:{user_id}:{organization_id}"

        cached = cache_service.get(cache_key)
        if cached is not None:
            if cached == "null":
                return None
            return TimeLogGetResponse(**cached)

        row: Optional[TimeLog] = None
        project_name = None
        task_title = None
        try:
            db = SyncSessionLocal()
            try:
                project_ids = list(
                    db.execute(
                        select(Project.id).where(Project.org_id == UUID(str(organization_id)))
                    ).scalars().all()
                )
                if not project_ids:
                    cache_service.set(cache_key, "null", ttl=self.CACHE_TTL_ACTIVE)
                    return None
                row = db.execute(
                    select(TimeLog)
                    .where(
                        TimeLog.created_by == UUID(str(user_id)),
                        TimeLog.status == TimeLogStatus.RUNNING.value,
                        TimeLog.project_id.in_(project_ids),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if row:
                    project_name = db.execute(
                        select(Project.name).where(Project.id == row.project_id)
                    ).scalar_one_or_none()
                    if row.task_id:
                        task_title = db.execute(
                            select(Task.title).where(Task.id == row.task_id)
                        ).scalar_one_or_none()
            finally:
                db.close()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get active time log: {str(e)}",
            )

        if not row:
            cache_service.set(cache_key, "null", ttl=self.CACHE_TTL_ACTIVE)
            return None

        log_date = row.date if row.date else datetime.now(timezone.utc).date()
        started_utc_dt = _parse_stored_to_utc_datetime(row.started_at, log_date)
        if not started_utc_dt:
            started_utc_dt = datetime.combine(log_date, time(0, 0, 0), tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed_duration = max(0, (now - started_utc_dt).total_seconds())

        user_tz = self._get_user_timezone(user_id)
        started_at_user_tz = self._stored_timestamp_to_user_time(row.started_at, user_tz, log_date)

        result = TimeLogGetResponse(
            id=row.id,
            project_id=row.project_id,
            task_id=row.task_id,
            started_at=started_at_user_tz,
            stoped_at=None,
            date=log_date,
            duration_seconds=elapsed_duration,
            duration_formatted=format_duration(elapsed_duration),
            status=row.status,
            notes=row.notes,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            project_name=project_name,
            task_title=task_title,
        )

        cache_service.set(cache_key, result.model_dump(mode='json'), ttl=self.CACHE_TTL_ACTIVE)

        return result
    
    def get_time_logs(
        self,
        organization_id: UUID4,
        user_id: Optional[UUID4] = None,
        project_id: Optional[UUID4] = None,
        task_id: Optional[UUID4] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        status_filter: Optional[TimeLogStatus] = None,
        limit: Optional[int] = 100,
        offset: Optional[int] = 0,
    ) -> TimeLogListResponse:
        # Build cache key
        cache_key = f"time_logs:list:{organization_id}:{user_id}:{project_id}:{task_id}:{from_date}:{to_date}:{status_filter}:{limit}:{offset}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return TimeLogListResponse(**cached)

        rows: List[TimeLog] = []
        projects_map: dict[str, str] = {}
        tasks_map: dict[str, str] = {}
        try:
            db = SyncSessionLocal()
            try:
                project_uuid_ids = list(
                    db.execute(
                        select(Project.id).where(Project.org_id == UUID(str(organization_id)))
                    ).scalars().all()
                )
                if not project_uuid_ids:
                    return TimeLogListResponse(
                        time_logs=[],
                        total_count=0,
                        total_duration_seconds=0,
                        total_duration_formatted=format_duration(0),
                    )
                id_strs = {str(x) for x in project_uuid_ids}
                if project_id:
                    if str(project_id) not in id_strs:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Project does not belong to this organization",
                        )
                    project_uuid_ids = [UUID(str(project_id))]

                filters = [TimeLog.project_id.in_(project_uuid_ids)]
                if user_id:
                    filters.append(TimeLog.created_by == UUID(str(user_id)))
                if task_id:
                    filters.append(TimeLog.task_id == UUID(str(task_id)))
                if from_date:
                    filters.append(TimeLog.date >= from_date)
                if to_date:
                    filters.append(TimeLog.date <= to_date)
                if status_filter:
                    filters.append(TimeLog.status == status_filter.value)
                base_where = and_(*filters)

                total_count = int(
                    db.execute(select(func.count()).select_from(TimeLog).where(base_where)).scalar_one()
                )

                list_q = select(TimeLog).where(base_where).order_by(TimeLog.created_at.desc())
                if limit is not None:
                    list_q = list_q.limit(limit)
                if offset:
                    list_q = list_q.offset(offset)
                rows = list(db.execute(list_q).scalars().all())

                puuids = list({r.project_id for r in rows})
                tuuids = list({r.task_id for r in rows if r.task_id})
                projects_map = {}
                if puuids:
                    for pid, pname in db.execute(
                        select(Project.id, Project.name).where(Project.id.in_(puuids))
                    ).all():
                        projects_map[str(pid)] = pname
                tasks_map = {}
                if tuuids:
                    for tid, title in db.execute(
                        select(Task.id, Task.title).where(Task.id.in_(tuuids))
                    ).all():
                        tasks_map[str(tid)] = title
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get time logs: {str(e)}",
            )

        user_tz = self._get_user_timezone(user_id) if user_id else "UTC"

        time_logs: List[TimeLogGetResponse] = []
        for log in rows:
            log_date = log.date if log.date else datetime.now(timezone.utc).date()
            started_at_user_tz = self._stored_timestamp_to_user_time(log.started_at, user_tz, log_date)
            stoped_at_user_tz = self._stored_timestamp_to_user_time(log.stoped_at, user_tz, log_date)
            time_logs.append(
                TimeLogGetResponse(
                    id=log.id,
                    project_id=log.project_id,
                    task_id=log.task_id,
                    started_at=started_at_user_tz,
                    stoped_at=stoped_at_user_tz,
                    date=log_date,
                    duration_seconds=log.duration_seconds,
                    duration_formatted=format_duration(log.duration_seconds),
                    status=log.status,
                    notes=log.notes,
                    created_by=log.created_by,
                    created_at=log.created_at,
                    updated_at=log.updated_at,
                    project_name=projects_map.get(str(log.project_id)),
                    task_title=tasks_map.get(str(log.task_id)) if log.task_id else None,
                )
            )

        total_duration = sum(log.duration_seconds for log in time_logs)
        
        result = TimeLogListResponse(
            time_logs=time_logs,
            total_count=total_count,
            total_duration_seconds=total_duration,
            total_duration_formatted=format_duration(total_duration),
        )
        
        # Cache the result
        cache_service.set(cache_key, result.model_dump(mode='json'), ttl=self.CACHE_TTL_LIST)
        
        return result
    
    def get_time_log(self, time_log_id: UUID4, user_id: UUID4, organization_id: UUID4) -> TimeLogGetResponse:
        cache_key = f"time_log:{time_log_id}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return TimeLogGetResponse(**cached)
        
        # Verify time log belongs to organization
        self._verify_time_log_organization(time_log_id, organization_id)
        
        try:
            db = SyncSessionLocal()
            try:
                log = db.execute(
                    select(TimeLog).where(TimeLog.id == UUID(str(time_log_id)))
                ).scalar_one_or_none()
                if not log:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Time log not found",
                    )
                if str(log.created_by) != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You don't have permission to view this time log",
                    )
                project_name = db.execute(
                    select(Project.name).where(Project.id == log.project_id)
                ).scalar_one_or_none()
                task_title = None
                if log.task_id:
                    task_title = db.execute(
                        select(Task.title).where(Task.id == log.task_id)
                    ).scalar_one_or_none()
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get time log: {str(e)}",
            )

        duration_seconds = log.duration_seconds
        user_tz = self._get_user_timezone(user_id)
        log_date = log.date if log.date else datetime.now(timezone.utc).date()
        started_at_user_tz = self._stored_timestamp_to_user_time(log.started_at, user_tz, log_date)
        stoped_at_user_tz = self._stored_timestamp_to_user_time(log.stoped_at, user_tz, log_date)

        result = TimeLogGetResponse(
            id=log.id,
            project_id=log.project_id,
            task_id=log.task_id,
            started_at=started_at_user_tz,
            stoped_at=stoped_at_user_tz,
            date=log_date,
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=log.status,
            notes=log.notes,
            created_by=log.created_by,
            created_at=log.created_at,
            updated_at=log.updated_at,
            project_name=project_name,
            task_title=task_title,
        )
        
        cache_service.set(cache_key, result.model_dump(mode='json'), ttl=self.CACHE_TTL_SINGLE)
        
        return result
    
    def update_time_log(
        self,
        time_log_id: UUID4,
        time_log_request: TimeLogUpdateRequest,
        user_id: UUID4,
        organization_id: UUID4,
    ) -> TimeLogUpdateResponse:
        # Verify time log belongs to organization
        self._verify_time_log_organization(time_log_id, organization_id)
        
        user_tz = self._get_user_timezone(user_id)
        log: Optional[TimeLog] = None
        try:
            db = SyncSessionLocal()
            try:
                row = db.execute(
                    select(TimeLog).where(TimeLog.id == UUID(str(time_log_id)))
                ).scalar_one_or_none()
                if not row:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Time log not found",
                    )
                if str(row.created_by) != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You don't have permission to update this time log",
                    )
                if row.status == TimeLogStatus.RUNNING.value:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot update a running time log. Please stop it first.",
                    )

                log_date = row.date if row.date else datetime.now(timezone.utc).date()
                changed = False
                if time_log_request.notes is not None:
                    row.notes = time_log_request.notes
                    changed = True
                if time_log_request.started_at is not None:
                    started_at_utc = self._convert_time_to_utc(
                        time_log_request.started_at, log_date, user_tz
                    )
                    row.started_at = datetime.combine(log_date, started_at_utc, tzinfo=timezone.utc)
                    changed = True
                if time_log_request.stoped_at is not None:
                    stoped_at_utc = self._convert_time_to_utc(
                        time_log_request.stoped_at, log_date, user_tz
                    )
                    row.stoped_at = datetime.combine(log_date, stoped_at_utc, tzinfo=timezone.utc)
                    changed = True
                if time_log_request.duration_seconds is not None:
                    row.duration_seconds = time_log_request.duration_seconds
                    changed = True
                if changed:
                    row.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(row)
                log = row
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update time log: {str(e)}",
            )

        if log is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time log not found",
            )

        duration_seconds = log.duration_seconds
        log_date = log.date if log.date else datetime.now(timezone.utc).date()
        started_at_user_tz = self._stored_timestamp_to_user_time(log.started_at, user_tz, log_date)
        stoped_at_user_tz = self._stored_timestamp_to_user_time(log.stoped_at, user_tz, log_date)

        result = TimeLogUpdateResponse(
            id=log.id,
            project_id=log.project_id,
            task_id=log.task_id,
            started_at=started_at_user_tz,
            stoped_at=stoped_at_user_tz,
            date=log_date,
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=log.status,
            notes=log.notes,
            created_by=log.created_by,
            created_at=log.created_at,
            updated_at=log.updated_at,
        )
        
        # Invalidate caches
        self._invalidate_time_log_caches(user_id, time_log_id, organization_id=organization_id)
        
        return result
    
    def delete_time_log(self, time_log_id: UUID4, user_id: UUID4, organization_id: UUID4) -> TimeLogDeleteResponse:
        # Verify time log belongs to organization
        self._verify_time_log_organization(time_log_id, organization_id)
        
        try:
            db = SyncSessionLocal()
            try:
                log = db.execute(
                    select(TimeLog).where(TimeLog.id == UUID(str(time_log_id)))
                ).scalar_one_or_none()
                if not log:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Time log not found",
                    )
                if str(log.created_by) != str(user_id):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You don't have permission to delete this time log",
                    )
                db.execute(delete(TimeLog).where(TimeLog.id == log.id))
                db.commit()
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete time log: {str(e)}",
            )
        
        # Invalidate caches
        self._invalidate_time_log_caches(user_id, time_log_id, organization_id=organization_id)
        
        return TimeLogDeleteResponse(
            success=True,
            message="Time log deleted successfully"
        )