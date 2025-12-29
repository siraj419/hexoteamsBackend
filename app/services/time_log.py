from pydantic import UUID4
from fastapi import HTTPException, status
from supabase_auth.errors import AuthApiError
from datetime import datetime, timezone, date, time, timedelta
from typing import Optional, List


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
from app.core import supabase
from app.utils.redis_cache import cache_service


def format_duration(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class TimeLogService:
    CACHE_TTL_ACTIVE = 30  # 30 seconds for active time log (real-time)
    CACHE_TTL_LIST = 180  # 3 minutes for lists
    CACHE_TTL_SINGLE = 300  # 5 minutes for single time log
    
    def __init__(self):
        pass
    
    def _invalidate_time_log_caches(self, user_id: UUID4, time_log_id: Optional[UUID4] = None, organization_id: Optional[UUID4] = None):
        """Invalidate time log caches"""
        cache_service.delete(f"time_log:active:{user_id}")
        if organization_id:
            cache_service.invalidate_pattern(f"time_logs:list:{organization_id}:*")
        else:
            cache_service.invalidate_pattern(f"time_logs:list:*")
        if time_log_id:
            cache_service.delete(f"time_log:{time_log_id}")
    
    def _verify_time_log_organization(self, time_log_id: UUID4, organization_id: UUID4) -> None:
        """Verify that a time log's project belongs to the organization"""
        try:
            # Get the time log's project_id
            time_log_response = supabase.table('time_logs').select('project_id').eq('id', str(time_log_id)).execute()
            if not time_log_response.data or len(time_log_response.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Time log not found"
                )
            
            project_id = time_log_response.data[0]['project_id']
            
            # Verify the project belongs to the organization
            project_response = supabase.table('projects').select('org_id').eq('id', str(project_id)).execute()
            if not project_response.data or len(project_response.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
            
            project_org_id = project_response.data[0]['org_id']
            if str(project_org_id) != str(organization_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Time log does not belong to this organization"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify time log organization: {str(e)}"
            )
    
    def start_time_log(
        self,
        time_log_request: TimeLogStartRequest,
        user_id: UUID4,
    ) -> TimeLogStartResponse:
        try:
            active_log = supabase.table('time_logs').select('*').eq('created_by', str(user_id)).eq('status', TimeLogStatus.RUNNING.value).execute()
            
            if active_log.data and len(active_log.data) > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="You already have an active time log running. Please stop it before starting a new one."
                )
            
            project_check = supabase.table('projects').select('id, org_id').eq('id', str(time_log_request.project_id)).execute()
            if not project_check.data or len(project_check.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
            organization_id = UUID4(project_check.data[0]['org_id'])
            
            task_check = supabase.table('tasks').select('id, project_id').eq('id', str(time_log_request.task_id)).execute()
            if not task_check.data or len(task_check.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            
            if str(task_check.data[0]['project_id']) != str(time_log_request.project_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Task does not belong to the specified project"
                )
            
            now = datetime.now(timezone.utc)
            current_time = now.time()
            current_date = now.date()
            
            response = supabase.table('time_logs').insert({
                'project_id': str(time_log_request.project_id),
                'task_id': str(time_log_request.task_id),
                'started_at': current_time.isoformat(),
                'date': current_date.isoformat(),
                'duration_seconds': 0,
                'status': TimeLogStatus.RUNNING.value,
                'notes': time_log_request.notes,
                'created_by': str(user_id),
                'created_at': now.isoformat(),
                'updated_at': now.isoformat(),
            }).execute()
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to start time log: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to start time log"
            )
        
        duration_seconds = response.data[0]['duration_seconds']
        result = TimeLogStartResponse(
            id=response.data[0]['id'],
            project_id=response.data[0]['project_id'],
            task_id=response.data[0]['task_id'],
            started_at=response.data[0]['started_at'],
            stoped_at=response.data[0].get('stoped_at'),
            date=response.data[0]['date'],
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=response.data[0]['status'],
            notes=response.data[0].get('notes'),
            created_by=response.data[0].get('created_by'),
            created_at=response.data[0]['created_at'],
            updated_at=response.data[0]['updated_at'],
        )
        
        # Invalidate caches
        self._invalidate_time_log_caches(user_id, organization_id=organization_id)
        
        return result
    
    def create_time_log(
        self,
        time_log_request: TimeLogCreateRequest,
        user_id: UUID4,
    ) -> TimeLogCreateResponse:
        try:
            project_check = supabase.table('projects').select('id, org_id').eq('id', str(time_log_request.project_id)).execute()
            if not project_check.data or len(project_check.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found"
                )
            organization_id = UUID4(project_check.data[0]['org_id'])
            
            task_check = supabase.table('tasks').select('id, project_id').eq('id', str(time_log_request.task_id)).execute()
            if not task_check.data or len(task_check.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            
            if str(task_check.data[0]['project_id']) != str(time_log_request.project_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Task does not belong to the specified project"
                )
            
            started_at = time_log_request.started_at
            stoped_at = time_log_request.stoped_at
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
                duration_seconds = (stopped_datetime - started_datetime).total_seconds()
            elif stoped_at is not None and duration_seconds is not None:
                started_datetime = datetime.combine(time_log_request.date, started_at)
                stopped_datetime = datetime.combine(time_log_request.date, stoped_at)
                if stopped_datetime < started_datetime:
                    stopped_datetime = datetime.combine(time_log_request.date, time(23, 59, 59))
                calculated_duration = (stopped_datetime - started_datetime).total_seconds()
                duration_seconds = calculated_duration
            
            now = datetime.now(timezone.utc)
            
            response = supabase.table('time_logs').insert({
                'project_id': str(time_log_request.project_id),
                'task_id': str(time_log_request.task_id),
                'started_at': started_at.isoformat(),
                'stoped_at': stoped_at.isoformat() if stoped_at else None,
                'date': time_log_request.date.isoformat(),
                'duration_seconds': duration_seconds,
                'status': TimeLogStatus.STOPPED.value,
                'notes': time_log_request.notes,
                'created_by': str(user_id),
                'created_at': now.isoformat(),
                'updated_at': now.isoformat(),
            }).execute()
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create time log: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create time log"
            )
        
        duration_seconds = response.data[0]['duration_seconds']
        result = TimeLogCreateResponse(
            id=response.data[0]['id'],
            project_id=response.data[0]['project_id'],
            task_id=response.data[0]['task_id'],
            started_at=response.data[0]['started_at'],
            stoped_at=response.data[0].get('stoped_at'),
            date=response.data[0]['date'],
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=response.data[0]['status'],
            notes=response.data[0].get('notes'),
            created_by=response.data[0].get('created_by'),
            created_at=response.data[0]['created_at'],
            updated_at=response.data[0]['updated_at'],
        )
        
        # Invalidate caches
        self._invalidate_time_log_caches(user_id, organization_id=organization_id)
        
        return result
    
    def stop_time_log(
        self,
        time_log_id: UUID4,
        stop_request: TimeLogStopRequest,
        user_id: UUID4,
    ) -> TimeLogStopResponse:
        try:
            existing_log = supabase.table('time_logs').select('*').eq('id', str(time_log_id)).execute()
            
            if not existing_log.data or len(existing_log.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Time log not found"
                )
            
            log = existing_log.data[0]
            
            if str(log['created_by']) != str(user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to stop this time log"
                )
            
            if log['status'] != TimeLogStatus.RUNNING.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Time log is not running"
                )
            
            # Get organization_id from project
            project_response = supabase.table('projects').select('org_id').eq('id', str(log['project_id'])).execute()
            if project_response.data and len(project_response.data) > 0:
                organization_id = UUID4(project_response.data[0]['org_id'])
            else:
                organization_id = None
            
            now = datetime.now(timezone.utc)
            current_time = now.time()
            
            started_at_str = log['started_at']
            if isinstance(started_at_str, str):
                started_time = datetime.strptime(started_at_str, "%H:%M:%S.%f").time() if '.' in started_at_str else datetime.strptime(started_at_str, "%H:%M:%S").time()
            else:
                started_time = started_at_str
            
            log_date = datetime.strptime(log['date'], "%Y-%m-%d").date() if isinstance(log['date'], str) else log['date']
            started_datetime = datetime.combine(log_date, started_time)
            stopped_datetime = datetime.combine(log_date, current_time)
            
            if stopped_datetime < started_datetime:
                stopped_datetime = datetime.combine(log_date, time(23, 59, 59))
            
            duration_seconds = (stopped_datetime - started_datetime).total_seconds()
            
            updates = {
                'stoped_at': current_time.isoformat(),
                'duration_seconds': duration_seconds,
                'status': TimeLogStatus.STOPPED.value,
                'updated_at': now.isoformat(),
            }
            
            if stop_request.notes is not None:
                updates['notes'] = stop_request.notes
            
            response = supabase.table('time_logs').update(updates).eq('id', str(time_log_id)).execute()
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to stop time log: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time log not found"
            )
        
        duration_seconds = response.data[0]['duration_seconds']
        result = TimeLogStopResponse(
            id=response.data[0]['id'],
            project_id=response.data[0]['project_id'],
            task_id=response.data[0]['task_id'],
            started_at=response.data[0]['started_at'],
            stoped_at=response.data[0].get('stoped_at'),
            date=response.data[0]['date'],
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=response.data[0]['status'],
            notes=response.data[0].get('notes'),
            created_by=response.data[0].get('created_by'),
            created_at=response.data[0]['created_at'],
            updated_at=response.data[0]['updated_at'],
        )
        
        # Invalidate caches
        self._invalidate_time_log_caches(user_id, time_log_id, organization_id=organization_id)
        
        return result
    
    def get_active_time_log(self, user_id: UUID4) -> Optional[TimeLogGetResponse]:
        cache_key = f"time_log:active:{user_id}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached is not None:
            if cached == "null":
                return None
            return TimeLogGetResponse(**cached)
        
        try:
            response = supabase.table('time_logs').select('*').eq('created_by', str(user_id)).eq('status', TimeLogStatus.RUNNING.value).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get active time log: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            # Cache None result
            cache_service.set(cache_key, "null", ttl=self.CACHE_TTL_ACTIVE)
            return None
        
        log = response.data[0]
        
        started_at_str = log['started_at']
        if isinstance(started_at_str, str):
            started_time = datetime.strptime(started_at_str, "%H:%M:%S.%f").time() if '.' in started_at_str else datetime.strptime(started_at_str, "%H:%M:%S").time()
        else:
            started_time = started_at_str
        
        log_date = datetime.strptime(log['date'], "%Y-%m-%d").date() if isinstance(log['date'], str) else log['date']
        started_datetime = datetime.combine(log_date, started_time)
        now = datetime.now(timezone.utc)
        current_time = now.time()
        current_datetime = datetime.combine(log_date, current_time)
        
        if current_datetime < started_datetime:
            current_datetime = datetime.combine(log_date, time(23, 59, 59))
        
        elapsed_duration = (current_datetime - started_datetime).total_seconds()
        
        result = TimeLogGetResponse(
            id=log['id'],
            project_id=log['project_id'],
            task_id=log['task_id'],
            started_at=log['started_at'],
            stoped_at=log.get('stoped_at'),
            date=log['date'],
            duration_seconds=elapsed_duration,
            duration_formatted=format_duration(elapsed_duration),
            status=log['status'],
            notes=log.get('notes'),
            created_by=log.get('created_by'),
            created_at=log['created_at'],
            updated_at=log['updated_at'],
        )
        
        # Cache the result
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
        
        # Get all project IDs for the organization
        try:
            projects_response = supabase.table('projects').select('id').eq('org_id', str(organization_id)).execute()
            if not projects_response.data:
                # No projects in organization, return empty result
                return TimeLogListResponse(
                    time_logs=[],
                    total_count=0,
                    total_duration_seconds=0,
                    total_duration_formatted=format_duration(0),
                )
            project_ids = [str(p['id']) for p in projects_response.data]
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization projects: {str(e)}"
            )
        
        # If a specific project_id is provided, verify it belongs to the organization
        if project_id:
            if str(project_id) not in project_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Project does not belong to this organization"
                )
            # Filter to only this project
            project_ids = [str(project_id)]
        
        query = supabase.table('time_logs').select('*', count='exact')
        
        # Filter by organization projects
        query = query.in_('project_id', project_ids)
        
        if user_id:
            query = query.eq('created_by', str(user_id))
        if task_id:
            query = query.eq('task_id', str(task_id))
        if from_date:
            query = query.gte('date', from_date.isoformat())
        if to_date:
            query = query.lte('date', to_date.isoformat())
        if status_filter:
            query = query.eq('status', status_filter.value)
        
        query = query.order('created_at', desc=True)
        
        if limit:
            query = query.limit(limit)
        if offset:
            query = query.offset(offset)
        
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get time logs: {str(e)}"
            )
        
        time_logs = [TimeLogGetResponse(
            id=log['id'],
            project_id=log['project_id'],
            task_id=log['task_id'],
            started_at=log['started_at'],
            stoped_at=log.get('stoped_at'),
            date=log['date'],
            duration_seconds=log['duration_seconds'],
            duration_formatted=format_duration(log['duration_seconds']),
            status=log['status'],
            notes=log.get('notes'),
            created_by=log.get('created_by'),
            created_at=log['created_at'],
            updated_at=log['updated_at'],
        ) for log in response.data] if response.data else []
        
        total_count = response.count if response.count else 0
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
            response = supabase.table('time_logs').select('*').eq('id', str(time_log_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get time log: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time log not found"
            )
        
        log = response.data[0]
        
        if str(log['created_by']) != str(user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view this time log"
            )
        
        duration_seconds = log['duration_seconds']
        result = TimeLogGetResponse(
            id=log['id'],
            project_id=log['project_id'],
            task_id=log['task_id'],
            started_at=log['started_at'],
            stoped_at=log.get('stoped_at'),
            date=log['date'],
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=log['status'],
            notes=log.get('notes'),
            created_by=log.get('created_by'),
            created_at=log['created_at'],
            updated_at=log['updated_at'],
        )
        
        # Cache the result
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
        
        try:
            existing_log = supabase.table('time_logs').select('*').eq('id', str(time_log_id)).execute()
            
            if not existing_log.data or len(existing_log.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Time log not found"
                )
            
            log = existing_log.data[0]
            
            if str(log['created_by']) != str(user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to update this time log"
                )
            
            if log['status'] == TimeLogStatus.RUNNING.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot update a running time log. Please stop it first."
                )
            
            updates = {}
            if time_log_request.notes is not None:
                updates['notes'] = time_log_request.notes
            if time_log_request.started_at is not None:
                updates['started_at'] = time_log_request.started_at.isoformat()
            if time_log_request.stoped_at is not None:
                updates['stoped_at'] = time_log_request.stoped_at.isoformat()
            if time_log_request.duration_seconds is not None:
                updates['duration_seconds'] = time_log_request.duration_seconds
            
            if updates:
                updates['updated_at'] = datetime.now(timezone.utc).isoformat()
                response = supabase.table('time_logs').update(updates).eq('id', str(time_log_id)).execute()
            else:
                response = existing_log
                
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update time log: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time log not found"
            )
        
        log = response.data[0]
        duration_seconds = log['duration_seconds']
        result = TimeLogUpdateResponse(
            id=log['id'],
            project_id=log['project_id'],
            task_id=log['task_id'],
            started_at=log['started_at'],
            stoped_at=log.get('stoped_at'),
            date=log['date'],
            duration_seconds=duration_seconds,
            duration_formatted=format_duration(duration_seconds),
            status=log['status'],
            notes=log.get('notes'),
            created_by=log.get('created_by'),
            created_at=log['created_at'],
            updated_at=log['updated_at'],
        )
        
        # Invalidate caches
        self._invalidate_time_log_caches(user_id, time_log_id, organization_id=organization_id)
        
        return result
    
    def delete_time_log(self, time_log_id: UUID4, user_id: UUID4, organization_id: UUID4) -> TimeLogDeleteResponse:
        # Verify time log belongs to organization
        self._verify_time_log_organization(time_log_id, organization_id)
        
        try:
            existing_log = supabase.table('time_logs').select('*').eq('id', str(time_log_id)).execute()
            
            if not existing_log.data or len(existing_log.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Time log not found"
                )
            
            log = existing_log.data[0]
            
            if str(log['created_by']) != str(user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to delete this time log"
                )
            
            response = supabase.table('time_logs').delete().eq('id', str(time_log_id)).execute()
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete time log: {str(e)}"
                )
                
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Time log not found"
            )
        
        # Invalidate caches
        self._invalidate_time_log_caches(user_id, time_log_id, organization_id=organization_id)
        
        return TimeLogDeleteResponse(
            success=True,
            message="Time log deleted successfully"
        )