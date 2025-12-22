from fastapi import HTTPException, status
from pydantic import UUID4
from typing import List, Optional
from datetime import datetime, timezone
import logging

from app.schemas.activities import (
    ActivityType,
    ActivityResponse,
    ActivityGetPaginatedResponse,
)
from app.services.files import FilesService
from app.core import supabase
from app.utils import calculate_time_ago, apply_pagination
from app.utils.redis_cache import ProjectSummaryCache

logger = logging.getLogger(__name__)

class ActivityService:
    def __init__(self, files_service: FilesService):
        self.files_service = files_service
        
    def add_activity(
        self,
        activity_type: ActivityType,
        entity_id: UUID4,
        actor_id: UUID4,
        description: str,
    ):
        # Get the profile ID (primary key) from the user_id
        try:
            profile_response = supabase.table('profiles').select('id').eq('user_id', str(actor_id)).execute()
            if not profile_response.data or len(profile_response.data) == 0:
                logger.warning(f"Profile not found for user_id {actor_id}, skipping activity insertion")
                return
            profile_id = profile_response.data[0]['id']
        except Exception as e:
            logger.error(f"Failed to get profile ID for user_id {actor_id}: {str(e)}", exc_info=True)
            return
        
        # insert the activity into the unified activities table with activity_type field
        try:
            insert_data = {
                'entity_id': str(entity_id),
                'actor_profile_id': str(profile_id),
                'activity_type': activity_type.value,
                'description': description,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
            response = supabase.table('activities').insert(insert_data).execute()
            logger.info(f"Activity inserted successfully: {response.data if hasattr(response, 'data') else 'No data returned'}")
        except Exception as e:
            logger.error(f"Failed to add activity: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add activity: {str(e)}"
            )
        
        # Invalidate project summary cache for activities
        if activity_type == ActivityType.PROJECT:
            # Project activity - invalidate cache for the project
            ProjectSummaryCache.delete_summary(str(entity_id))
        elif activity_type == ActivityType.TASK:
            # Task activity - get project_id from task and invalidate cache
            try:
                task_response = supabase.table('tasks').select('project_id').eq('id', str(entity_id)).execute()
                if task_response.data and len(task_response.data) > 0:
                    project_id = task_response.data[0].get('project_id')
                    if project_id:
                        ProjectSummaryCache.delete_summary(str(project_id))
            except Exception as e:
                logger.warning(f"Failed to get project_id for task activity: {str(e)}")
        
        return

    def get_activities(
        self,
        entity_id: UUID4,
        activity_type: ActivityType,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[ActivityResponse]:
        # For task activities: get only activities with activity_type='task' for that task
        # For project activities: get both activity_type='project' for project AND activity_type='task' for tasks in that project
        if activity_type == ActivityType.TASK:
            query = supabase.table('activities').select(
                'id, description, created_at, activity_type, profiles(display_name, avatar_file_id, timezone)'
            ).eq('entity_id', str(entity_id)).eq('activity_type', 'task').order('created_at', desc=True)
        else:
            # For project activities, get all task IDs for this project first
            try:
                tasks_response = supabase.table('tasks').select('id').eq('project_id', str(entity_id)).execute()
                task_ids = [str(task['id']) for task in tasks_response.data] if tasks_response.data else []
            except Exception as e:
                logger.error(f"Failed to get tasks for project {entity_id}: {str(e)}")
                task_ids = []
            
            # Get project activities and task activities separately, then combine
            all_activities = []
            
            # Get project activities (no pagination yet, we'll combine and paginate after)
            try:
                project_activities = supabase.table('activities').select(
                    'id, description, created_at, activity_type, profiles(display_name, avatar_file_id, timezone)'
                ).eq('entity_id', str(entity_id)).eq('activity_type', 'project').order('created_at', desc=True).execute()
                if project_activities.data:
                    all_activities.extend(project_activities.data)
            except Exception as e:
                logger.error(f"Failed to get project activities: {str(e)}")
            
            # Get task activities for tasks in this project (no pagination yet)
            if task_ids:
                try:
                    task_activities = supabase.table('activities').select(
                        'id, description, created_at, activity_type, profiles(display_name, avatar_file_id, timezone)'
                    ).eq('activity_type', 'task').in_('entity_id', task_ids).order('created_at', desc=True).execute()
                    if task_activities.data:
                        all_activities.extend(task_activities.data)
                except Exception as e:
                    logger.error(f"Failed to get task activities: {str(e)}")
            
            # Sort by created_at descending and apply pagination manually
            all_activities.sort(key=lambda x: x['created_at'], reverse=True)
            
            # Apply pagination
            total_count = len(all_activities)
            if offset is not None and limit is not None:
                all_activities = all_activities[offset:offset + limit]
            elif offset is not None:
                all_activities = all_activities[offset:]
            elif limit is not None:
                all_activities = all_activities[:limit]
            
            activities = []
            for activity in all_activities:
                avatar_url = None
                profile = activity.get('profiles', {})
                if profile and profile.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(profile['avatar_file_id'])
                    except Exception:
                        pass
                    
                activities.append(ActivityResponse(
                    id=activity['id'],
                    user_display_name=profile.get('display_name', 'Unknown') if profile else 'Unknown',
                    user_avatar_url=avatar_url,
                    description=activity['description'],
                    activity_time=calculate_time_ago(activity['created_at'], profile.get('timezone', 'utc') if profile else 'utc'),
                ))
            
            return activities
        
        # apply the pagination
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get activities: {e}"
            )
        
        activities = []
        for activity in response.data:
            avatar_url = None
            profile = activity.get('profiles', {})
            if profile and profile.get('avatar_file_id'):
                try:
                    avatar_url = self.files_service.get_file_url(profile['avatar_file_id'])
                except Exception:
                    pass
                
            activities.append(ActivityResponse(
                id=activity['id'],
                user_display_name=profile.get('display_name', 'Unknown') if profile else 'Unknown',
                user_avatar_url=avatar_url,
                description=activity['description'],
                activity_time=calculate_time_ago(activity['created_at'], profile.get('timezone', 'utc') if profile else 'utc'),
            ))
        
        return activities
    
    def get_activities_paginated(
        self,
        entity_id: UUID4,
        activity_type: ActivityType,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ActivityGetPaginatedResponse:
        """
        Get paginated activities for an entity.
        For tasks: returns only task activities (activity_type='task')
        For projects: returns both project activities and task activities for tasks in that project
        Optimized to fetch all data in a single query with profile join.
        """
        if activity_type == ActivityType.TASK:
            # Get only task activities for this task
            query = supabase.table('activities').select(
                'id, description, created_at, activity_type, profiles(display_name, avatar_file_id, timezone)',
                count='exact'
            ).eq('entity_id', str(entity_id)).eq('activity_type', 'task').order('created_at', desc=True)
        else:
            # For project activities, get all task IDs for this project first
            try:
                tasks_response = supabase.table('tasks').select('id').eq('project_id', str(entity_id)).execute()
                task_ids = [str(task['id']) for task in tasks_response.data] if tasks_response.data else []
            except Exception as e:
                logger.error(f"Failed to get tasks for project {entity_id}: {str(e)}")
                task_ids = []
            
            # Get project activities and task activities separately, then combine
            all_activities = []
            
            # Get project activities (no pagination yet, we'll combine and paginate after)
            try:
                project_activities = supabase.table('activities').select(
                    'id, description, created_at, activity_type, profiles(display_name, avatar_file_id, timezone)'
                ).eq('entity_id', str(entity_id)).eq('activity_type', 'project').order('created_at', desc=True).execute()
                if project_activities.data:
                    all_activities.extend(project_activities.data)
            except Exception as e:
                logger.error(f"Failed to get project activities: {str(e)}")
            
            # Get task activities for tasks in this project (no pagination yet)
            if task_ids:
                try:
                    task_activities = supabase.table('activities').select(
                        'id, description, created_at, activity_type, profiles(display_name, avatar_file_id, timezone)'
                    ).eq('activity_type', 'task').in_('entity_id', task_ids).order('created_at', desc=True).execute()
                    if task_activities.data:
                        all_activities.extend(task_activities.data)
                except Exception as e:
                    logger.error(f"Failed to get task activities: {str(e)}")
            
            # Sort by created_at descending (merge sort since both are already sorted)
            all_activities.sort(key=lambda x: x['created_at'], reverse=True)
            
            # Apply pagination manually
            total_count = len(all_activities)
            if offset is not None and limit is not None:
                all_activities = all_activities[offset:offset + limit]
            elif offset is not None:
                all_activities = all_activities[offset:]
            elif limit is not None:
                all_activities = all_activities[:limit]
            
            activities = []
            for activity in all_activities:
                avatar_url = None
                profile = activity.get('profiles', {})
                if profile and profile.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(profile['avatar_file_id'])
                    except Exception:
                        pass
                    
                activities.append(ActivityResponse(
                    id=activity['id'],
                    user_display_name=profile.get('display_name', '') if profile else '',
                    user_avatar_url=avatar_url,
                    description=activity['description'],
                    activity_time=calculate_time_ago(activity['created_at'], profile.get('timezone', 'utc') if profile else 'utc'),
                ))
            
            return ActivityGetPaginatedResponse(
                activities=activities,
                total=total_count,
                offset=offset,
                limit=limit,
            )
        
        # For task activities, use the query approach
        # apply the pagination
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get activities: {e}"
            )
        
        total_count = response.count if hasattr(response, 'count') and response.count is not None else len(response.data) if response.data else 0
        
        activities = []
        for activity in response.data:
            avatar_url = None
            profile = activity.get('profiles', {})
            if profile and profile.get('avatar_file_id'):
                try:
                    avatar_url = self.files_service.get_file_url(profile['avatar_file_id'])
                except Exception:
                    pass
                
            activities.append(ActivityResponse(
                id=activity['id'],
                user_display_name=profile.get('display_name', '') if profile else '',
                user_avatar_url=avatar_url,
                description=activity['description'],
                activity_time=calculate_time_ago(activity['created_at'], profile.get('timezone', 'utc') if profile else 'utc'),
            ))
        
        return ActivityGetPaginatedResponse(
            activities=activities,
            total=total_count,
            offset=offset,
            limit=limit,
        )

    def delete_activity(
        self,
        activity_id: UUID4,
    ) -> bool:
        try:
            supabase.table('activities').delete().eq('id', activity_id).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete activity: {e}"
            )
        return True
    
    def delete_all(
        self,
        entity_id: UUID4,
        activity_type: ActivityType,
    ) -> bool:
        try:
            supabase.table('activities').delete().eq('entity_id', str(entity_id)).eq('activity_type', activity_type.value).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete all activities: {e}"
            )
        return True