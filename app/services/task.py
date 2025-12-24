import re
import logging
from uuid import UUID
from fastapi import HTTPException, status, UploadFile
from pydantic import UUID4
from supabase_auth.errors import AuthApiError
from typing import Optional, List, Callable, Dict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from app.core import supabase
from app.schemas.tasks import (
    TaskCreateRequest,
    TaskCreateResponse,
    TaskUpdateRequest,
    TaskUpdateResponse,
    TaskResponse,
    TaskStatus,
    TaskCreateAttachmentResponse,
    TaskGetAttachmentResponse,
    TaskGetAttachmentWithUrlResponse,
    TaskCommentCreateRequest,
    TaskCommentUpdateRequest,
    TaskCommentCreateResponse,
    TaskCommentUpdateResponse,
    TaskCommentAttachmentResponse,
    TaskGetCommentResponse,
    TaskGetCommentsPaginatedResponse,
    TaskUserInfoResponse,
    TaskGetResponse,
    TaskBaseResponse,
    TaskChangeAssigneeRequest,
    TaskChangeStatusRequest,
    TaskUpdateDetailsRequest,
)
from app.schemas.attachments import AttachmentType, AttachmentResponse

from app.services.files import FilesService
from app.services.attachment import AttachmentService
from app.services.activity import ActivityService, ActivityType
from app.services.link import LinkService, LinkEntityType
from app.utils import calculate_time_ago, apply_pagination, calculate_file_size
from app.utils.redis_cache import ProjectSummaryCache
from app.utils.inbox_helpers import (
    trigger_task_assigned_notification,
    trigger_task_unassigned_notification,
    trigger_task_completed_notification,
)
from app.core import settings

class TaskService:
    def __init__(self):
        self.files_service = FilesService()
        self.attachment_service = AttachmentService(self.files_service)
        self.activity_service = ActivityService(self.files_service)
            
    def create_task(
        self,
        task_request: TaskCreateRequest,
        user_id: UUID4,
        project_id: UUID4,
        parent_id: Optional[UUID4] = None,
    ) -> TaskCreateResponse:
        
        if parent_id:
            depth = self._get_depth(settings.MAX_SUBTASK_DEPTH, parent_id, lambda x: self._get_parent_id('tasks', x))
            if depth == -1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Subtask depth exceed the maximum allowed depth",
                )
        
        try:
            insert_data = {
                'title': task_request.title,
                'content': task_request.content,
                'status': task_request.status.value,
                'parent_id': str(parent_id) if parent_id else None,
                'project_id': str(project_id),
                'created_by': str(user_id),
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            
            if task_request.due_date:
                if isinstance(task_request.due_date, datetime):
                    insert_data['due_date'] = task_request.due_date.isoformat()
                else:
                    insert_data['due_date'] = task_request.due_date
            
            if task_request.assignee_id:
                insert_data['assignee_id'] = str(task_request.assignee_id)
            
            response = supabase.table('tasks').insert(insert_data).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create task: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create task"
            )
            
        # add the task attachments
        if task_request.file_ids and len(task_request.file_ids) > 0:
            for file_id in task_request.file_ids:
                self.attachment_service.add_attachment(AttachmentType.TASk, response.data[0]['id'], file_id)
        
        # Record activity: task created
        try:
            user_info = self._get_user_info(user_id)
            self.activity_service.add_activity(
                ActivityType.TASK,
                response.data[0]['id'],
                user_id,
                f"Task created by {user_info.display_name}"
            )
        except Exception as e:
            # Don't fail task creation if activity recording fails, but log the error
            logger.error(f"Failed to record task creation activity: {str(e)}", exc_info=True)
        
        # Invalidate project summary cache
        ProjectSummaryCache.delete_summary(str(project_id))
            
        return TaskCreateResponse(
            id=response.data[0]['id'],
            parent_id=response.data[0]['parent_id'],
            title=response.data[0]['title'],
            content=response.data[0]['content'],
            status=response.data[0]['status'],
            due_date=response.data[0]['due_date'],
            assignee_id=response.data[0]['assignee_id'],
            project_id=response.data[0]['project_id'],
        )

    
    def add_task_comment(
        self,
        task_id: UUID4,
        task_comment_request: TaskCommentCreateRequest,
        user_id: UUID4,
        parent_id: Optional[UUID4] = None,
    ) -> TaskCommentCreateResponse:
        
        if parent_id:
            depth = self._get_depth(settings.MAX_COMMENT_REPLY_DEPTH, parent_id, lambda x: self._get_parent_id('task_comments', x))
            if depth == -1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Comment depth exceed the maximum allowed depth",
                )
        
        try:
            response = supabase.table('task_comments').insert({
                'task_id': str(task_id),
                'content': task_comment_request.content,
                'parent_id': str(parent_id) if parent_id else None,
                'created_by': str(user_id),
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            if e.code == '23505':
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to add task comment for task {task_id}, task or user not found",
                )
            
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add task comment: {e}"
            )
        
        # add the task comment attachments
        attachments = []
        if task_comment_request.file_ids and len(task_comment_request.file_ids) > 0:
            for file_id in task_comment_request.file_ids:
                attachment = self.attachment_service.add_attachment(AttachmentType.COMMENT, response.data[0]['id'], file_id)
                attachments.append(attachment)
        
        user_timezone = self._get_user_timezone(user_id)
        user_info = self._get_user_info(user_id)
        
        return TaskCommentCreateResponse(
            id=response.data[0]['id'],
            content=response.data[0]['content'],
            comment_by=user_info,
            message_time=calculate_time_ago(response.data[0]['created_at'], user_timezone),
            attachments=attachments,
        )
    
    def update_task_comment(
        self,
        comment_id: UUID4,
        comment_update_request: TaskCommentUpdateRequest,
        user_id: UUID4,
    ) -> TaskCommentUpdateResponse:
        try:
            response = supabase.table('task_comments').update({
                'content': comment_update_request.content,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', str(comment_id)).eq('created_by', str(user_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update task comment: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found or you don't have permission to update it"
            )
        
        # Add new attachments if provided
        attachments = []
        if comment_update_request.file_ids and len(comment_update_request.file_ids) > 0:
            for file_id in comment_update_request.file_ids:
                attachment = self.attachment_service.add_attachment(AttachmentType.COMMENT, comment_id, file_id)
                attachments.append(attachment)
        
        # Get existing attachments
        existing_attachments = self.attachment_service.get_attachments(AttachmentType.COMMENT, comment_id)
        attachments.extend(existing_attachments.attachments)
        
        user_timezone = self._get_user_timezone(user_id)
        user_info = self._get_user_info(user_id)
        
        return TaskCommentUpdateResponse(
            id=response.data[0]['id'],
            content=response.data[0]['content'],
            comment_by=user_info,
            message_time=calculate_time_ago(response.data[0]['updated_at'], user_timezone),
            attachments=attachments,
        )
    
    def delete_task_comment(
        self,
        comment_id: UUID4,
        user_id: UUID4,
    ) -> bool:
        # Check if comment exists and belongs to user
        try:
            response = supabase.table('task_comments').select('id, file_ids').eq('id', str(comment_id)).eq('created_by', str(user_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task comment: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found or you don't have permission to delete it"
            )
        
        # Delete associated attachments
        try:
            attachments = self.attachment_service.get_attachments(AttachmentType.COMMENT, comment_id)
            for attachment in attachments.attachments:
                self.attachment_service.delete_attachment(attachment.id)
        except Exception:
            pass  # Continue even if attachment deletion fails
        
        # Delete the comment
        try:
            supabase.table('task_comments').delete().eq('id', str(comment_id)).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete task comment: {e}"
            )
        
        return True
    
    def get_task(
        self,
        user_id: UUID4,
        task_id: UUID4,
    ) -> TaskResponse:
        try:
            response = supabase.table('tasks').select('*').eq('id', task_id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task: {e}"
            )

            
        user_timezone = self._get_user_timezone(user_id)
        link_service = LinkService(user_timezone)
        task_comments, _ = self._get_task_comments(task_id, user_timezone)
        task_attachments_response = self.attachment_service.get_attachments(AttachmentType.TASk, task_id)
        task_attachments = task_attachments_response.attachments
        task_activities = self.activity_service.get_activities(task_id, ActivityType.TASK)
        task_links_response = link_service.get_links(task_id, LinkEntityType.TASK)
        task_links = task_links_response.links
        assignee = self._get_user_info(response.data[0]['assignee_id']) if response.data[0]['assignee_id'] else None
        sub_tasks = self._get_sub_tasks(task_id)
            
        
        return TaskGetResponse(
            id=response.data[0]['id'],
            title=response.data[0]['title'],
            content=response.data[0]['content'],
            status=response.data[0]['status'],
            due_date=response.data[0]['due_date'],
            assignee=assignee,
            project_id=response.data[0]['project_id'],
            comments=task_comments,
            attachments=task_attachments,
            activities=task_activities,
            links=task_links,
            sub_tasks=sub_tasks,
        )
    
    def list_tasks(
        self,
        project_id: UUID4,
        user_id: Optional[UUID4] = None,
        search: Optional[str] = None,
        assignee_id: Optional[UUID4] = None,
        status: Optional[TaskStatus] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[TaskResponse]:
        
        
        query = supabase.table('tasks').select('*').eq('project_id', str(project_id)).is_('parent_id', 'null')
        
        if user_id:
            query = query.eq('created_by', str(user_id))
        if assignee_id:
            query = query.eq('assignee_id', str(assignee_id))
        if status:
            query = query.eq('status', status.value)
        if search:
            query = query.ilike('title', f'%{search}%')
        
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get tasks: {e}"
            )
        
        return [TaskResponse(
            id=task['id'],
            title=task['title'],
            content=task['content'],
            status=task['status'],
            due_date=task['due_date'],
            assignee=self._get_user_info(task['assignee_id']) if task['assignee_id'] else None,
        ) for task in response.data]
    
    def change_task_assignee(
        self,
        task_id: UUID4,
        assignee_request: TaskChangeAssigneeRequest,
        user_id: UUID4,
    ) -> TaskResponse:
        """
        Change, assign, or unassign the assignee of a task.
        Optimized single query operation.
        - To assign: provide assignee_id
        - To unassign: provide None
        - To change: provide different assignee_id
        Task can only have exactly one assignee (or none).
        Records activity when assignee changes.
        """
        # Get current task data to compare
        try:
            current_task = supabase.table('tasks').select('assignee_id').eq('id', str(task_id)).execute()
            if not current_task.data or len(current_task.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            old_assignee_id = current_task.data[0].get('assignee_id')
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task: {e}"
            )
        
        try:
            update_data = {
                'assignee_id': str(assignee_request.assignee_id) if assignee_request.assignee_id else None,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            
            response = supabase.table('tasks').update(update_data).eq('id', str(task_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to change task assignee: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )
        
        # Record activity: assignee changed
        try:
            actor_info = self._get_user_info(user_id)
            new_assignee_id = response.data[0].get('assignee_id')
            task_data = response.data[0]
            
            task_title = task_data.get('title', 'Untitled Task')
            project_id = task_data.get('project_id')
            
            project_name = "Unknown Project"
            org_id = None
            if project_id:
                try:
                    project_response = supabase.table('projects').select('name, organization_id').eq('id', str(project_id)).execute()
                    if project_response.data:
                        project_name = project_response.data[0].get('name', 'Unknown Project')
                        org_id = project_response.data[0].get('organization_id')
                except Exception:
                    pass
            
            if not old_assignee_id and new_assignee_id:
                # Task assigned
                assignee_info = self._get_user_info(new_assignee_id)
                description = f"Task assigned to {assignee_info.display_name} by {actor_info.display_name}"
                
                if org_id and str(new_assignee_id) != str(user_id):
                    trigger_task_assigned_notification(
                        user_id=UUID4(new_assignee_id),
                        org_id=UUID4(org_id),
                        task_id=task_id,
                        task_title=task_title,
                        assigned_by_id=user_id,
                        assigned_by_name=actor_info.display_name,
                        project_name=project_name,
                    )
            elif old_assignee_id and not new_assignee_id:
                # Task unassigned
                old_assignee_info = self._get_user_info(old_assignee_id)
                description = f"Task unassigned from {old_assignee_info.display_name} by {actor_info.display_name}"
                
                if org_id and str(old_assignee_id) != str(user_id):
                    trigger_task_unassigned_notification(
                        user_id=UUID4(old_assignee_id),
                        org_id=UUID4(org_id),
                        task_id=task_id,
                        task_title=task_title,
                        unassigned_by_id=user_id,
                        unassigned_by_name=actor_info.display_name,
                        project_name=project_name,
                    )
            elif old_assignee_id and new_assignee_id and old_assignee_id != new_assignee_id:
                # Assignee changed
                old_assignee_info = self._get_user_info(old_assignee_id)
                new_assignee_info = self._get_user_info(new_assignee_id)
                description = f"Task assignee changed from {old_assignee_info.display_name} to {new_assignee_info.display_name} by {actor_info.display_name}"
                
                if org_id:
                    if str(old_assignee_id) != str(user_id):
                        trigger_task_unassigned_notification(
                            user_id=UUID4(old_assignee_id),
                            org_id=UUID4(org_id),
                            task_id=task_id,
                            task_title=task_title,
                            unassigned_by_id=user_id,
                            unassigned_by_name=actor_info.display_name,
                            project_name=project_name,
                        )
                    
                    if str(new_assignee_id) != str(user_id):
                        trigger_task_assigned_notification(
                            user_id=UUID4(new_assignee_id),
                            org_id=UUID4(org_id),
                            task_id=task_id,
                            task_title=task_title,
                            assigned_by_id=user_id,
                            assigned_by_name=actor_info.display_name,
                            project_name=project_name,
                        )
            else:
                # No change, skip activity
                description = None
            
            if description:
                self.activity_service.add_activity(
                    ActivityType.TASK,
                    task_id,
                    user_id,
                    description
                )
        except Exception as e:
            # Don't fail assignee change if activity recording fails, but log the error
            logger.error(f"Failed to record assignee change activity: {str(e)}", exc_info=True)
        
        # Invalidate project summary cache (affects team workload)
        project_id = response.data[0].get('project_id')
        if project_id:
            ProjectSummaryCache.delete_summary(str(project_id))
        
        assignee = None
        if response.data[0].get('assignee_id'):
            assignee = self._get_user_info(response.data[0]['assignee_id'])
        
        return TaskResponse(
            id=response.data[0]['id'],
            title=response.data[0]['title'],
            content=response.data[0]['content'],
            status=response.data[0]['status'],
            due_date=response.data[0]['due_date'],
            assignee=assignee,
        )
    
    def get_task_assignee(
        self,
        task_id: UUID4,
    ) -> Optional[TaskUserInfoResponse]:
        """
        Get the assignee user info for a task.
        Returns None if task has no assignee.
        Optimized single query operation.
        """
        try:
            response = supabase.table('tasks').select('assignee_id').eq('id', str(task_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task assignee: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )
        
        assignee_id = response.data[0].get('assignee_id')
        if not assignee_id:
            return None
        
        return self._get_user_info(assignee_id)
    
    def update_task(
        self,
        task_id: UUID4,
        task_request: TaskUpdateRequest,
        user_id: UUID4,
    ) -> TaskUpdateResponse:
        # Get current task data to compare status
        try:
            current_task = supabase.table('tasks').select('status').eq('id', str(task_id)).execute()
            if not current_task.data or len(current_task.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            old_status = current_task.data[0].get('status')
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task: {e}"
            )
        
        try:
            response = supabase.table('tasks').update({
                'title': task_request.title,
                'content': task_request.content,
                'status': task_request.status.value if task_request.status else None,
                'due_date': task_request.due_date,
                'assignee_id': task_request.assignee_id,
                'updated_at': datetime.now(timezone.utc),
            }).eq('id', task_id).eq('created_by', user_id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update task: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to update task"
            )
        
        # Record activity: status changed
        if task_request.status and old_status != task_request.status.value:
            try:
                actor_info = self._get_user_info(user_id)
                self.activity_service.add_activity(
                    ActivityType.TASK,
                    task_id,
                    user_id,
                    f"Task status changed from {old_status} to {task_request.status.value} by {actor_info.display_name}"
                )
            except Exception as e:
                # Don't fail task update if activity recording fails, but log the error
                logger.error(f"Failed to record status change activity: {str(e)}", exc_info=True)
        
        # Invalidate project summary cache
        project_id = response.data[0].get('project_id')
        if project_id:
            ProjectSummaryCache.delete_summary(str(project_id))
        
        return TaskUpdateResponse(
            id=response.data[0]['id'],
            title=response.data[0]['title'],
            content=response.data[0]['content'],
            status=response.data[0]['status'],
            due_date=response.data[0]['due_date'],
            assignee_id=response.data[0]['assignee_id'],
            project_id=response.data[0]['project_id'],
        )
    
    def change_task_status(
        self,
        task_id: UUID4,
        status_request: TaskChangeStatusRequest,
        user_id: UUID4,
    ) -> TaskResponse:
        """
        Change the status of a task.
        Records activity when status changes.
        Optimized single query operation.
        """
        # Get current task data to compare
        try:
            current_task = supabase.table('tasks').select('status').eq('id', str(task_id)).execute()
            if not current_task.data or len(current_task.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            old_status = current_task.data[0].get('status')
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task: {e}"
            )
        
        try:
            response = supabase.table('tasks').update({
                'status': status_request.status.value,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', str(task_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to change task status: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )
        
        # Record activity: status changed
        if old_status != status_request.status.value:
            print("Changing status....")
            try:
                actor_info = self._get_user_info(user_id)
                self.activity_service.add_activity(
                    ActivityType.TASK,
                    task_id,
                    user_id,
                    f"Task status changed from {old_status} to {status_request.status.value} by {actor_info.display_name}"
                )

                
                if status_request.status.value == 'completed':
                    print("Task completed....")
                    task_data = response.data[0]
                    task_title = task_data.get('title', 'Untitled Task')
                    project_id = task_data.get('project_id')
                    
                    project_name = "Unknown Project"
                    org_id = None
                    if project_id:
                        try:
                            project_response = supabase.table('projects').select('name, org_id').eq('id', str(project_id)).execute()
                            if project_response.data:
                                project_name = project_response.data[0].get('name', 'Unknown Project')
                                org_id = project_response.data[0].get('org_id')
                            print(project_response)
                        except Exception as e:
                            print(f"Failed to get project: {e}")
                    
                    if org_id and project_id:
                        print("Triggering task completed notification....")
                        trigger_task_completed_notification(
                            project_id=UUID4(project_id),
                            org_id=UUID4(org_id),
                            task_id=task_id,
                            task_title=task_title,
                            completed_by_id=user_id,
                            completed_by_name=actor_info.display_name,
                            project_name=project_name,
                        )
            except Exception as e:
                # Don't fail status change if activity recording fails, but log the error
                logger.error(f"Failed to record status change activity: {str(e)}", exc_info=True)
        
        # Invalidate project summary cache
        project_id = response.data[0].get('project_id')
        if project_id:
            ProjectSummaryCache.delete_summary(str(project_id))
        
        assignee = None
        if response.data[0].get('assignee_id'):
            assignee = self._get_user_info(response.data[0]['assignee_id'])
        
        return TaskResponse(
            id=response.data[0]['id'],
            title=response.data[0]['title'],
            content=response.data[0]['content'],
            status=response.data[0]['status'],
            due_date=response.data[0]['due_date'],
            assignee=assignee,
        )
    
    def update_task_details(
        self,
        task_id: UUID4,
        details_request: TaskUpdateDetailsRequest,
        user_id: UUID4,
    ) -> TaskResponse:
        """
        Update task details (title, content, and/or due_date).
        Optimized single query operation.
        """
        if not details_request.title and not details_request.content and not details_request.due_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one of title, content, or due_date must be provided"
            )
        
        update_data = {
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        
        if details_request.title is not None:
            update_data['title'] = details_request.title
        if details_request.content is not None:
            update_data['content'] = details_request.content
        if details_request.due_date is not None:
            update_data['due_date'] = details_request.due_date.isoformat() if isinstance(details_request.due_date, datetime) else details_request.due_date
        
        try:
            response = supabase.table('tasks').update(update_data).eq('id', str(task_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update task details: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )
        
        # Invalidate project summary cache
        project_id = response.data[0].get('project_id')
        if project_id:
            ProjectSummaryCache.delete_summary(str(project_id))
        
        assignee = None
        if response.data[0].get('assignee_id'):
            assignee = self._get_user_info(response.data[0]['assignee_id'])
        
        return TaskResponse(
            id=response.data[0]['id'],
            title=response.data[0]['title'],
            content=response.data[0]['content'],
            status=response.data[0]['status'],
            due_date=response.data[0]['due_date'],
            assignee=assignee,
        )

    def delete_task(
        self,
        task_id: UUID4,
        user_id: UUID4,
        force_delete: bool = False,
    ) -> bool:
        """
        Delete a task.
        
        Args:
            task_id: ID of the task to delete
            user_id: ID of the user attempting to delete
            force_delete: If True, bypasses creator check (for org admins/owners)
        
        Returns:
            bool: True if deletion was successful
        """
        # Get project_id before deleting
        project_id = None
        try:
            task_response = supabase.table('tasks').select('project_id').eq('id', str(task_id)).execute()
            if task_response.data and len(task_response.data) > 0:
                project_id = task_response.data[0].get('project_id')
        except Exception:
            pass
        
        try:
            # If force_delete is True (org admin/owner), don't filter by created_by
            if force_delete:
                response = supabase.table('tasks').delete().eq('id', str(task_id)).execute()
            else:
                response = supabase.table('tasks').delete().eq('id', str(task_id)).eq('created_by', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete task: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found or already deleted"
            )
        
        # Invalidate project summary cache
        if project_id:
            ProjectSummaryCache.delete_summary(str(project_id))
        
        return True
    
    def list_subtasks(
        self,
        task_id: UUID4,
        search: Optional[str] = None,
        user_id: Optional[UUID4] = None,
        assignee_id: Optional[UUID4] = None,
        status: Optional[TaskStatus] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[TaskResponse]:
        """
        List all subtasks for a given task with filters and pagination.
        Optimized to batch fetch assignee info.
        """
        query = supabase.table('tasks').select('*').eq('parent_id', str(task_id))
        
        if user_id:
            query = query.eq('created_by', str(user_id))
        if assignee_id:
            query = query.eq('assignee_id', str(assignee_id))
        if status:
            query = query.eq('status', status.value)
        if search:
            query = query.ilike('title', f'%{search}%')
        
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get subtasks: {e}"
            )
        
        if not response.data:
            return []
        
        all_assignee_ids = set()
        for task in response.data:
            if task.get('assignee_id'):
                all_assignee_ids.add(task['assignee_id'])
        
        assignee_cache = {}
        if all_assignee_ids:
            assignee_cache = self._batch_get_user_info([UUID4(uid) if isinstance(uid, str) else uid for uid in all_assignee_ids])
        
        return [TaskResponse(
            id=task['id'],
            title=task['title'],
            content=task['content'],
            status=task['status'],
            due_date=task['due_date'],
            assignee=assignee_cache.get(str(task['assignee_id'])) if task.get('assignee_id') else None,
        ) for task in response.data]
    
    def get_task_attachments(
        self,
        task_id: UUID4,
        user_id: Optional[UUID4] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[TaskGetAttachmentResponse]:
        query = supabase.table('task_attachments').select('*').eq('task_id', task_id)
        if user_id:
            query = query.eq('created_by', user_id)
        if limit:
            query = query.limit(limit)
        if offset:
            query = query.offset(offset)
            
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task attachments: {e}"
            )
        
        attachments = []
        for attachment in response.data:
            file_data = self.files_service.get_file_with_url(attachment['file_id'])
            attachments.append(TaskGetAttachmentResponse(
                id=attachment['id'],
                file_id=attachment['file_id'],
                file_name=file_data['file']['name'],
                task_id=attachment['task_id'],
                created_at=attachment['created_at'],
                updated_at=attachment['updated_at'],
                file_url=file_data['file_url'],
            ))
        
        return attachments
    
    def get_task_attachment_with_url(
        self,
        attachment_id: UUID4,
    ) -> TaskGetAttachmentWithUrlResponse:
        try:
            response = supabase.table('task_attachments').select('*').eq('id', attachment_id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task attachment with url: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task attachment not found"
            )
            
        # get the file data and url
        file_data = self.files_service.get_file_with_url(response.data[0]['file_id'])
        file_url = self.files_service.get_file_url(response.data[0]['file_id'])
        
        return TaskGetAttachmentWithUrlResponse(
            id=response.data[0]['id'],
            file_id=response.data[0]['file_id'],
            file_name=response.data[0]['file_name'],
            task_id=response.data[0]['task_id'],
            created_at=response.data[0]['created_at'],
            updated_at=response.data[0]['updated_at'],
            file_url=file_url,
        )
    
    def get_task_comments(
        self,
        task_id: UUID4,
        user_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> TaskGetCommentsPaginatedResponse:
        """
        Get all base comments for a task with their replies and attachments.
        Only returns top-level comments (not replies), with replies nested under their parents.
        Returns paginated response with total count.
        """
        user_timezone = self._get_user_timezone(user_id)
        comments, total_count = self._get_task_comments(task_id, user_timezone, limit, offset)
        
        return TaskGetCommentsPaginatedResponse(
            comments=comments,
            total=total_count,
            offset=offset,
            limit=limit,
        )
    
    def _get_task_comment_replies(
        self,
        comment_id: UUID4,
        user_timezone: str,
        user_info_cache: Dict[str, TaskUserInfoResponse],
    ) -> List[TaskGetCommentResponse]:
        """
        Recursively get all replies for a comment with their attachments and nested subreplies
        Uses cached user info to avoid repeated queries.
        """
        try:
            response = supabase.table('task_comments').select('id, content, created_by, created_at').eq('parent_id', str(comment_id)).order('created_at', desc=False).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get comment replies: {e}"
            )
        
        if not response.data:
            return []
        
        replies = []
        for reply in response.data:
            user_id_str = str(reply['created_by'])
            user_info = user_info_cache.get(user_id_str)
            if not user_info:
                user_info = self._get_user_info(reply['created_by'])
                user_info_cache[user_id_str] = user_info
            
            subreplies = self._get_task_comment_replies(reply['id'], user_timezone, user_info_cache)
            attachments = self._batch_get_attachments([str(reply['id'])])
            
            replies.append(TaskGetCommentResponse(
                id=reply['id'],
                content=reply['content'],
                comment_by=user_info,
                message_time=calculate_time_ago(reply['created_at'], user_timezone),
                attachments=attachments.get(str(reply['id']), []),
                replies=subreplies,
            ))
        
        return replies
    
    def _get_task_comments(
        self,
        task_id: UUID4,
        user_timezone: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> tuple[List[TaskGetCommentResponse], int]:
        """
        Get the task comments for a task with their attachments and replies
        Returns tuple of (comments, total_count)
        Highly optimized: fetches all data in minimal queries and builds tree in memory.
        """
        
        query = supabase.table('task_comments').select('id, content, created_by, created_at', count='exact').eq('task_id', str(task_id)).is_('parent_id', 'null')
        
        limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task comments: {e}"
            )
        
        total_count = response.count if hasattr(response, 'count') and response.count is not None else len(response.data) if response.data else 0
        
        if not response.data:
            return [], total_count
        
        base_comment_ids = [str(comment['id']) for comment in response.data]
        
        all_user_ids = set()
        for comment in response.data:
            if comment.get('created_by'):
                all_user_ids.add(comment['created_by'])
        
        comments_by_parent = self._get_all_comments_for_task(task_id, base_comment_ids)
        
        for parent_id, comments in comments_by_parent.items():
            for comment in comments:
                if comment.get('created_by'):
                    all_user_ids.add(comment['created_by'])
        
        all_comment_ids = []
        for comments in comments_by_parent.values():
            all_comment_ids.extend([str(c['id']) for c in comments])
        all_comment_ids.extend(base_comment_ids)
        
        user_info_cache = self._batch_get_user_info([UUID4(uid) if isinstance(uid, str) else uid for uid in all_user_ids])
        attachments_by_comment = self._batch_get_attachments(all_comment_ids)
        
        comments = []
        for comment in response.data:
            comments.append(
                self._build_comment_tree(
                    comment,
                    comments_by_parent,
                    attachments_by_comment,
                    user_info_cache,
                    user_timezone,
                )
            )
        
        return comments, total_count
    
    def _get_all_comments_for_task(
        self,
        task_id: UUID4,
        base_comment_ids: List[str],
    ) -> Dict[str, List[Dict]]:
        """
        Fetch all comments (replies) for a task in a single query.
        Returns a dictionary mapping parent_id to list of comment dicts.
        """
        if not base_comment_ids:
            return {}
        
        try:
            # Get all comments that are replies to the base comments or their descendants
            # We'll fetch all comments for this task that have a parent_id
            response = supabase.table('task_comments').select('id, content, created_by, created_at, parent_id').eq('task_id', str(task_id)).not_.is_('parent_id', 'null').order('created_at', desc=False).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get all comments for task: {e}"
            )
        
        # Organize comments by parent_id
        comments_by_parent: Dict[str, List[Dict]] = {}
        for comment in response.data:
            parent_id = str(comment['parent_id'])
            if parent_id not in comments_by_parent:
                comments_by_parent[parent_id] = []
            comments_by_parent[parent_id].append(comment)
        
        return comments_by_parent
    
    def _batch_get_attachments(
        self,
        comment_ids: List[str],
    ) -> Dict[str, List[AttachmentResponse]]:
        """
        Batch fetch attachments for multiple comments in a single query.
        Returns a dictionary mapping comment_id to list of attachments.
        """
        if not comment_ids:
            return {}
        
        try:
            response = supabase.table('attachments').select('id, file_id, entity_id, files(name, size_bytes, content_type)').eq('entity_type', AttachmentType.COMMENT.value).in_('entity_id', comment_ids).execute()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to batch get attachments: {e}"
            )
        
        attachments_by_comment: Dict[str, List[AttachmentResponse]] = {}
        for attachment in response.data:
            comment_id = str(attachment['entity_id'])
            if comment_id not in attachments_by_comment:
                attachments_by_comment[comment_id] = []
            
            attachments_by_comment[comment_id].append(AttachmentResponse(
                id=attachment['id'],
                file_id=attachment['file_id'],
                file_name=attachment['files']['name'],
                file_size=calculate_file_size(attachment['files']['size_bytes']),
                content_type=attachment['files']['content_type'],
            ))
        
        return attachments_by_comment
    
    def _build_comment_tree(
        self,
        comment_data: Dict,
        comments_by_parent: Dict[str, List[Dict]],
        attachments_by_comment: Dict[str, List[AttachmentResponse]],
        user_info_cache: Dict[str, TaskUserInfoResponse],
        user_timezone: str,
    ) -> TaskGetCommentResponse:
        """
        Recursively build a comment tree from in-memory data.
        """
        comment_id = str(comment_data['id'])
        user_id_str = str(comment_data['created_by'])
        
        user_info = user_info_cache.get(user_id_str)
        if not user_info:
            user_info = self._get_user_info(comment_data['created_by'])
            user_info_cache[user_id_str] = user_info
        
        # Get replies for this comment
        replies_data = comments_by_parent.get(comment_id, [])
        replies = []
        for reply_data in replies_data:
            replies.append(
                self._build_comment_tree(
                    reply_data,
                    comments_by_parent,
                    attachments_by_comment,
                    user_info_cache,
                    user_timezone,
                )
            )
        
        return TaskGetCommentResponse(
            id=comment_data['id'],
            content=comment_data['content'],
            comment_by=user_info,
            message_time=calculate_time_ago(comment_data['created_at'], user_timezone),
            attachments=attachments_by_comment.get(comment_id, []),
            replies=replies,
        )
    
    def _get_sub_tasks(
        self,
        task_id: UUID4,
    ) -> List[TaskBaseResponse]:
        try:
            response = supabase.table('tasks').select('id, title, content, status, due_date, assignee_id, project_id').eq('parent_id', str(task_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get subtasks: {e}"
            )
        
        if not response.data:
            return []
        
        return [TaskBaseResponse(
            id=task['id'],
            title=task['title'],
            content=task['content'],
            status=task['status'],
            due_date=task['due_date'],
            assignee_id=task['assignee_id'],
            project_id=task['project_id'],
        ) for task in response.data]
    
    def _get_user_info(
        self,
        user_id: UUID4,
    ) -> TaskUserInfoResponse:
        try:
            response = supabase.table('profiles').select('user_id, display_name, avatar_file_id').eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user info: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        avatar_url = None
        if response.data[0].get('avatar_file_id'):
            try:
                avatar_url = self.files_service.get_file_url(response.data[0]['avatar_file_id'])
            except Exception:
                pass
        
        return TaskUserInfoResponse(
            id=response.data[0]['user_id'],
            display_name=response.data[0]['display_name'],
            avatar_url=avatar_url,
        )
    
    def _batch_get_user_info(
        self,
        user_ids: List[UUID4],
    ) -> Dict[str, TaskUserInfoResponse]:
        """
        Batch fetch user info for multiple user IDs and return as a dictionary.
        Optimized to fetch all profiles in a single query.
        """
        if not user_ids:
            return {}
        
        try:
            user_id_strings = [str(uid) if isinstance(uid, UUID) else uid for uid in user_ids]
            response = supabase.table('profiles').select('user_id, display_name, avatar_file_id').in_('user_id', user_id_strings).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to batch get user info: {e}"
            )
        
        user_info_dict = {}
        for profile in response.data:
            avatar_url = None
            if profile.get('avatar_file_id'):
                try:
                    avatar_url = self.files_service.get_file_url(profile['avatar_file_id'])
                except Exception:
                    pass
            
            user_info_dict[str(profile['user_id'])] = TaskUserInfoResponse(
                id=profile['user_id'],
                display_name=profile['display_name'],
                avatar_url=avatar_url,
            )
        
        return user_info_dict
    
    def _get_user_timezone(
        self,
        user_id: UUID4,
    ) -> str:
        try:
            response = supabase.table('profiles').select('timezone').eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user timezone: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            return 'utc'
        
        return response.data[0].get('timezone', 'utc')
    
    def _get_depth(
        self,
        max_depth: int,
        entity_id: UUID4,
        get_parent: Callable[[UUID4], Optional[UUID4]],
    ) -> int:
        depth = 0
        current_id = entity_id
        
        while depth < max_depth:
            parent_id = get_parent(current_id)
            if parent_id is None:
                return depth
            depth += 1
            current_id = parent_id
        
        return -1
    
    def _get_parent_id(
        self,
        table_name: str,
        entity_id: UUID4,
    ) -> Optional[UUID4]:
        try:
            response = supabase.table(table_name).select('parent_id').eq('id', str(entity_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get parent id: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            return None
        
        parent_id = response.data[0].get('parent_id')
        if not parent_id:
            return None
        
        return UUID4(parent_id)
