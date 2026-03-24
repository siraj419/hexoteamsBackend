import re
import logging
from uuid import UUID

from fastapi import HTTPException, status, UploadFile
from pydantic import UUID4
from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from typing import Optional, List, Callable, Dict, Any, Tuple
from datetime import datetime, timezone, date

logger = logging.getLogger(__name__)

from app.db.sync_session import SyncSessionLocal
from app.models import Attachment, File, Profile, Project, Task, TaskComment
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
    PaginatedAttachments,
    PaginatedLinks,
    PaginatedSubtasks,
)
from app.schemas.attachments import AttachmentType, AttachmentResponse

from app.services.files import FilesService
from app.services.attachment import AttachmentService
from app.schemas.activities import ActivityType
from app.services.activity import ActivityService
from app.services.link import LinkService, LinkEntityType
from app.utils import calculate_time_ago, calculate_file_size
from app.utils.sa_pagination import apply_sa_limit_offset
from app.utils.redis_cache import ProjectSummaryCache, cache_service
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
    
    def _format_status(self, status: str) -> str:
        """Format status value: remove underscores and convert to title case."""
        return status.replace('_', ' ').title()
            
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
        
        now = datetime.now(timezone.utc)
        due = task_request.due_date
        if due is not None and isinstance(due, date) and not isinstance(due, datetime):
            due = datetime.combine(due, datetime.min.time()).replace(tzinfo=timezone.utc)

        db = SyncSessionLocal()
        try:
            row = Task(
                title=task_request.title,
                content=task_request.content,
                status=task_request.status.value,
                parent_id=UUID(str(parent_id)) if parent_id else None,
                project_id=UUID(str(project_id)),
                created_by=UUID(str(user_id)),
                created_at=now,
                updated_at=now,
                due_date=due,
                assignee_id=UUID(str(task_request.assignee_id)) if task_request.assignee_id else None,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create task: {e}"
            )
        finally:
            db.close()

        if task_request.file_ids:
            for file_id in task_request.file_ids:
                self.attachment_service.add_attachment(AttachmentType.TASk, row.id, file_id)

        try:
            user_info = self._get_user_info(user_id)
            self.activity_service.add_activity(
                ActivityType.TASK,
                row.id,
                user_id,
                f"Task created by {user_info.display_name}"
            )
        except Exception as e:
            logger.error(f"Failed to record task creation activity: {str(e)}", exc_info=True)

        ProjectSummaryCache.delete_summary(str(project_id))

        try:
            from app.services.project import ProjectService
            project_service = ProjectService()
            project_service.update_project_progress(project_id)
        except Exception as e:
            logger.error(f"Failed to update project progress: {e}", exc_info=True)

        return TaskCreateResponse(
            id=row.id,
            parent_id=row.parent_id,
            title=row.title,
            content=row.content,
            status=TaskStatus(row.status),
            due_date=row.due_date,
            assignee_id=row.assignee_id,
            project_id=row.project_id,
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
        
        now = datetime.now(timezone.utc)
        db = SyncSessionLocal()
        try:
            row = TaskComment(
                task_id=UUID(str(task_id)),
                content=task_comment_request.content,
                parent_id=UUID(str(parent_id)) if parent_id else None,
                created_by=UUID(str(user_id)),
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        except IntegrityError as e:
            db.rollback()
            if getattr(e.orig, "pgcode", None) == "23505":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to add task comment for task {task_id}, task or user not found",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add task comment: {e}"
            )
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to add task comment: {e}"
            )
        finally:
            db.close()

        attachments = []
        if task_comment_request.file_ids:
            for file_id in task_comment_request.file_ids:
                attachment = self.attachment_service.add_attachment(AttachmentType.COMMENT, row.id, file_id)
                attachments.append(attachment)

        user_timezone = self._get_user_timezone(user_id)
        user_info = self._get_user_info(user_id)

        return TaskCommentCreateResponse(
            id=row.id,
            content=row.content,
            comment_by=user_info,
            message_time=calculate_time_ago(row.created_at, user_timezone),
            attachments=attachments,
        )
    
    def update_task_comment(
        self,
        comment_id: UUID4,
        comment_update_request: TaskCommentUpdateRequest,
        user_id: UUID4,
    ) -> TaskCommentUpdateResponse:
        now = datetime.now(timezone.utc)
        db = SyncSessionLocal()
        try:
            r = db.execute(
                update(TaskComment)
                .where(
                    TaskComment.id == UUID(str(comment_id)),
                    TaskComment.created_by == UUID(str(user_id)),
                )
                .values(content=comment_update_request.content, updated_at=now)
            )
            db.commit()
            if r.rowcount == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Comment not found or you don't have permission to update it"
                )
            row = db.get(TaskComment, UUID(str(comment_id)))
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update task comment: {e}"
            )
        finally:
            db.close()
        
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
            id=row.id,
            content=row.content,
            comment_by=user_info,
            message_time=calculate_time_ago(row.updated_at, user_timezone),
            attachments=attachments,
        )
    
    def delete_task_comment(
        self,
        comment_id: UUID4,
        user_id: UUID4,
    ) -> bool:
        db = SyncSessionLocal()
        try:
            row = db.execute(
                select(TaskComment.id).where(
                    TaskComment.id == UUID(str(comment_id)),
                    TaskComment.created_by == UUID(str(user_id)),
                )
            ).scalar_one_or_none()
        except Exception as e:
            db.close()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task comment: {e}"
            )

        if row is None:
            db.close()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found or you don't have permission to delete it"
            )

        try:
            self.attachment_service.delete_all(comment_id, AttachmentType.COMMENT)
        except Exception as e:
            logger.warning(f"Failed to delete attachments for comment {comment_id}: {e}")

        try:
            db.execute(delete(TaskComment).where(TaskComment.id == UUID(str(comment_id))))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete task comment: {e}"
            )
        finally:
            db.close()
        
        return True
    
    def get_task(
        self,
        user_id: UUID4,
        task_id: UUID4,
        attachments_limit: Optional[int] = 5,
        attachments_offset: Optional[int] = 0,
        links_limit: Optional[int] = 5,
        links_offset: Optional[int] = 0,
        subtasks_limit: Optional[int] = 5,
        subtasks_offset: Optional[int] = 0,
    ) -> TaskGetResponse:
        db = SyncSessionLocal()
        try:
            t = db.get(Task, UUID(str(task_id)))
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task: {e}"
            )
        finally:
            db.close()

        if t is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )

        user_timezone = self._get_user_timezone(user_id)
        link_service = LinkService(user_timezone)
        task_comments, _ = self._get_task_comments(task_id, user_timezone)
        task_attachments_response = self.attachment_service.get_attachments(
            AttachmentType.TASk, task_id, limit=attachments_limit, offset=attachments_offset
        )
        task_activities = self.activity_service.get_activities(task_id, ActivityType.TASK)
        task_links_response = link_service.get_links(
            task_id, LinkEntityType.TASK, limit=links_limit, offset=links_offset
        )
        assignee = self._get_user_info(t.assignee_id) if t.assignee_id else None

        subtasks_response = self.list_subtasks(
            task_id, user_id=user_id, limit=subtasks_limit, offset=subtasks_offset
        )

        return TaskGetResponse(
            id=t.id,
            title=t.title,
            content=t.content,
            status=TaskStatus(t.status),
            due_date=t.due_date,
            assignee=assignee,
            project_id=t.project_id,
            comments=task_comments,
            attachments_paginated=PaginatedAttachments(
                attachments=task_attachments_response.attachments,
                total=task_attachments_response.total,
                offset=task_attachments_response.offset,
                limit=task_attachments_response.limit,
            ),
            activities=task_activities,
            links_paginated=PaginatedLinks(
                links=task_links_response.links,
                total=task_links_response.total,
                offset=task_links_response.offset,
                limit=task_links_response.limit,
            ),
            sub_tasks_paginated=PaginatedSubtasks(
                subtasks=subtasks_response.subtasks,
                total=subtasks_response.total,
                offset=subtasks_response.offset,
                limit=subtasks_response.limit,
            ),
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
        
        stmt = select(Task).where(
            Task.project_id == UUID(str(project_id)),
            Task.parent_id.is_(None),
        )
        if user_id:
            stmt = stmt.where(Task.created_by == UUID(str(user_id)))
        if assignee_id:
            stmt = stmt.where(Task.assignee_id == UUID(str(assignee_id)))
        if status:
            stmt = stmt.where(Task.status == status.value)
        if search:
            stmt = stmt.where(Task.title.ilike(f"%{search}%"))

        lim, off, stmt = apply_sa_limit_offset(stmt, limit, offset)
        db = SyncSessionLocal()
        try:
            rows = list(db.scalars(stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get tasks: {e}"
            )
        finally:
            db.close()

        return [
            TaskResponse(
                id=r.id,
                title=r.title,
                content=r.content,
                status=TaskStatus(r.status),
                due_date=r.due_date,
                assignee=self._get_user_info(r.assignee_id) if r.assignee_id else None,
            )
            for r in rows
        ]
    
    def get_project_tasks_minimal(
        self,
        project_id: UUID4,
    ) -> List['TaskMinimalResponse']:
        """
        Get all tasks (including subtasks) for a project with minimal data (id and title only).
        Used for time log task selection.
        """
        from app.schemas.tasks import TaskMinimalResponse
        
        db = SyncSessionLocal()
        try:
            rows = db.execute(
                select(Task.id, Task.title)
                .where(Task.project_id == UUID(str(project_id)))
                .order_by(Task.created_at.asc())
            ).all()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get project tasks: {e}"
            )
        finally:
            db.close()

        if not rows:
            return []

        return [TaskMinimalResponse(id=r[0], title=r[1]) for r in rows]
    
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
        now = datetime.now(timezone.utc)
        tid = UUID(str(task_id))
        db = SyncSessionLocal()
        try:
            cur = db.get(Task, tid)
            if not cur:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            old_assignee_id = cur.assignee_id
            cur.assignee_id = UUID(str(assignee_request.assignee_id)) if assignee_request.assignee_id else None
            cur.updated_at = now
            db.commit()
            db.refresh(cur)
            task_row = cur
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to change task assignee: {e}"
            )
        finally:
            db.close()

        try:
            actor_info = self._get_user_info(user_id)
            new_assignee_id = task_row.assignee_id
            task_title = task_row.title or "Untitled Task"
            proj_id = task_row.project_id

            project_name = "Unknown Project"
            org_id = None
            if proj_id:
                pdb = SyncSessionLocal()
                try:
                    p = pdb.get(Project, proj_id)
                    if p:
                        project_name = p.name or "Unknown Project"
                        org_id = p.org_id
                except Exception:
                    pass
                finally:
                    pdb.close()
            
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
            logger.error(f"Failed to record assignee change activity: {str(e)}", exc_info=True)

        if task_row.project_id:
            ProjectSummaryCache.delete_summary(str(task_row.project_id))

        assignee = None
        if task_row.assignee_id:
            assignee = self._get_user_info(task_row.assignee_id)

        return TaskResponse(
            id=task_row.id,
            title=task_row.title,
            content=task_row.content,
            status=TaskStatus(task_row.status),
            due_date=task_row.due_date,
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
        db = SyncSessionLocal()
        try:
            t = db.get(Task, UUID(str(task_id)))
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task assignee: {e}"
            )
        finally:
            db.close()

        if t is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )

        if not t.assignee_id:
            return None

        return self._get_user_info(t.assignee_id)
    
    def update_task(
        self,
        task_id: UUID4,
        task_request: TaskUpdateRequest,
        user_id: UUID4,
    ) -> TaskUpdateResponse:
        tid = UUID(str(task_id))
        uid = UUID(str(user_id))
        now = datetime.now(timezone.utc)
        db = SyncSessionLocal()
        try:
            cur = db.get(Task, tid)
            if not cur or cur.created_by != uid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Failed to update task"
                )
            old_status = cur.status
            if task_request.title is not None:
                cur.title = task_request.title
            if task_request.content is not None:
                cur.content = task_request.content
            if task_request.status is not None:
                cur.status = task_request.status.value
            if task_request.due_date is not None:
                cur.due_date = task_request.due_date
            if task_request.assignee_id is not None:
                cur.assignee_id = task_request.assignee_id
            cur.updated_at = now
            db.commit()
            db.refresh(cur)
            updated = cur
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update task: {e}"
            )
        finally:
            db.close()
        
        status_changed = False
        if task_request.status and old_status != task_request.status.value:
            status_changed = True
            try:
                actor_info = self._get_user_info(user_id)
                self.activity_service.add_activity(
                    ActivityType.TASK,
                    task_id,
                    user_id,
                    f"Task status changed from {self._format_status(old_status)} to {self._format_status(task_request.status.value)} by {actor_info.display_name}"
                )
            except Exception as e:
                logger.error(f"Failed to record status change activity: {str(e)}", exc_info=True)

        pid = updated.project_id
        if pid:
            ProjectSummaryCache.delete_summary(str(pid))

            if status_changed and task_request.status:
                try:
                    from app.services.project import ProjectService
                    project_service = ProjectService()
                    project_service.update_project_progress(UUID4(pid))
                except Exception as e:
                    logger.error(f"Failed to update project progress: {e}", exc_info=True)

        return TaskUpdateResponse(
            id=updated.id,
            title=updated.title,
            content=updated.content,
            status=TaskStatus(updated.status),
            due_date=updated.due_date,
            assignee_id=updated.assignee_id,
            project_id=updated.project_id,
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
        tid = UUID(str(task_id))
        now = datetime.now(timezone.utc)
        db = SyncSessionLocal()
        try:
            cur = db.get(Task, tid)
            if not cur:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            old_status = cur.status
            cur.status = status_request.status.value
            cur.updated_at = now
            db.commit()
            db.refresh(cur)
            task_row = cur
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to change task status: {e}"
            )
        finally:
            db.close()

        if old_status != status_request.status.value:
            try:
                actor_info = self._get_user_info(user_id)
                self.activity_service.add_activity(
                    ActivityType.TASK,
                    task_id,
                    user_id,
                    f"Task status changed from {self._format_status(old_status)} to {self._format_status(status_request.status.value)} by {actor_info.display_name}"
                )

                if status_request.status.value == "completed":
                    task_title = task_row.title or "Untitled Task"
                    project_id = task_row.project_id
                    project_name = "Unknown Project"
                    org_id = None
                    if project_id:
                        pdb = SyncSessionLocal()
                        try:
                            p = pdb.get(Project, project_id)
                            if p:
                                project_name = p.name or "Unknown Project"
                                org_id = p.org_id
                        except Exception:
                            pass
                        finally:
                            pdb.close()

                    if org_id and project_id:
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
                logger.error(f"Failed to record status change activity: {str(e)}", exc_info=True)

        if task_row.project_id:
            ProjectSummaryCache.delete_summary(str(task_row.project_id))

            if old_status != status_request.status.value:
                try:
                    from app.services.project import ProjectService
                    project_service = ProjectService()
                    project_service.update_project_progress(UUID4(task_row.project_id))
                except Exception as e:
                    logger.error(f"Failed to update project progress: {e}", exc_info=True)

        assignee = None
        if task_row.assignee_id:
            assignee = self._get_user_info(task_row.assignee_id)

        return TaskResponse(
            id=task_row.id,
            title=task_row.title,
            content=task_row.content,
            status=TaskStatus(task_row.status),
            due_date=task_row.due_date,
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

        tid = UUID(str(task_id))
        now = datetime.now(timezone.utc)
        db = SyncSessionLocal()
        try:
            cur = db.get(Task, tid)
            if not cur:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found"
                )
            cur.updated_at = now
            if details_request.title is not None:
                cur.title = details_request.title
            if details_request.content is not None:
                cur.content = details_request.content
            if details_request.due_date is not None:
                dd = details_request.due_date
                if isinstance(dd, date) and not isinstance(dd, datetime):
                    dd = datetime.combine(dd, datetime.min.time()).replace(tzinfo=timezone.utc)
                cur.due_date = dd
            db.commit()
            db.refresh(cur)
            row = cur
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update task details: {e}"
            )
        finally:
            db.close()

        if row.project_id:
            ProjectSummaryCache.delete_summary(str(row.project_id))

        assignee = None
        if row.assignee_id:
            assignee = self._get_user_info(row.assignee_id)

        return TaskResponse(
            id=row.id,
            title=row.title,
            content=row.content,
            status=TaskStatus(row.status),
            due_date=row.due_date,
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
        tid = UUID(str(task_id))
        uid = UUID(str(user_id))
        project_id = None
        db = SyncSessionLocal()
        try:
            t = db.get(Task, tid)
            if t:
                project_id = t.project_id
            if force_delete:
                q = delete(Task).where(Task.id == tid)
            else:
                q = delete(Task).where(and_(Task.id == tid, Task.created_by == uid))
            r = db.execute(q)
            db.commit()
            if r.rowcount == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Task not found or already deleted"
                )
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete task: {e}"
            )
        finally:
            db.close()
        
        # Invalidate project summary cache and update project progress
        if project_id:
            ProjectSummaryCache.delete_summary(str(project_id))
            
            # Update project progress when task is deleted
            try:
                from app.services.project import ProjectService
                project_service = ProjectService()
                project_service.update_project_progress(UUID4(project_id))
            except Exception as e:
                logger.error(f"Failed to update project progress: {e}", exc_info=True)
        
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
    ):
        """
        List all subtasks for a given task with filters and pagination.
        Optimized to batch fetch assignee info.
        Returns paginated response with total count.
        """
        from app.schemas.tasks import TaskSubtasksPaginatedResponse

        pid = UUID(str(task_id))
        conds: List[Any] = [Task.parent_id == pid]
        if user_id:
            conds.append(Task.created_by == UUID(str(user_id)))
        if assignee_id:
            conds.append(Task.assignee_id == UUID(str(assignee_id)))
        if status:
            conds.append(Task.status == status.value)
        if search:
            conds.append(Task.title.ilike(f"%{search}%"))
        filt = and_(*conds)

        lim, off = limit, offset
        db = SyncSessionLocal()
        try:
            total_count = db.scalar(select(func.count()).select_from(Task).where(filt)) or 0
            stmt = select(Task).where(filt).order_by(Task.created_at.desc())
            if limit is not None or offset is not None:
                off = offset if offset is not None else settings.DEFAULT_PAGINATION_OFFSET
                lim = limit if limit is not None else settings.DEFAULT_PAGINATION_LIMIT
                stmt = stmt.limit(lim).offset(off)
            rows = list(db.scalars(stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get subtasks: {e}"
            )
        finally:
            db.close()

        if not rows:
            return TaskSubtasksPaginatedResponse(
                subtasks=[],
                total=total_count,
                offset=off,
                limit=lim,
            )

        all_assignee_ids = {r.assignee_id for r in rows if r.assignee_id}
        assignee_cache = {}
        if all_assignee_ids:
            assignee_cache = self._batch_get_user_info([UUID4(str(uid)) for uid in all_assignee_ids])

        subtasks = [
            TaskResponse(
                id=r.id,
                title=r.title,
                content=r.content,
                status=TaskStatus(r.status),
                due_date=r.due_date,
                assignee=assignee_cache.get(str(r.assignee_id)) if r.assignee_id else None,
            )
            for r in rows
        ]

        return TaskSubtasksPaginatedResponse(
            subtasks=subtasks,
            total=total_count,
            offset=off,
            limit=lim,
        )
    
    @staticmethod
    def _coerce_task_status(raw: str) -> TaskStatus:
        try:
            return TaskStatus(raw)
        except ValueError:
            return TaskStatus.TODO

    def get_user_tasks(
        self,
        user_id: UUID4,
        org_id: UUID4,
        task_type: str = "all",
        search: Optional[str] = None,
        task_status: Optional[TaskStatus] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ):
        from app.schemas.tasks import TasksPaginatedResponse

        oid, uid = UUID(str(org_id)), UUID(str(user_id))
        db = SyncSessionLocal()
        try:
            pids = list(db.scalars(select(Project.id).where(Project.org_id == oid)).all())
            if not pids:
                return TasksPaginatedResponse(
                    tasks=[],
                    total=0,
                    offset=offset,
                    limit=limit,
                )

            base = select(Task).where(
                Task.project_id.in_(pids),
                Task.parent_id.is_(None),
            )
            if task_type == "assigned":
                stmt_f = base.where(Task.assignee_id == uid)
            elif task_type == "created":
                stmt_f = base.where(Task.created_by == uid)
            elif task_type == "all":
                stmt_f = base.where(
                    or_(Task.assignee_id == uid, Task.created_by == uid)
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="task_type must be 'all', 'assigned', or 'created'",
                )
            if task_status:
                stmt_f = stmt_f.where(Task.status == task_status.value)
            if search:
                stmt_f = stmt_f.where(Task.title.ilike(f"%{search}%"))

            total_count = (
                db.scalar(select(func.count()).select_from(stmt_f.subquery())) or 0
            )
            ordered = stmt_f.order_by(Task.created_at.desc())
            lim, off, page_stmt = apply_sa_limit_offset(ordered, limit, offset)
            rows = list(db.scalars(page_stmt).all())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get tasks: {e}",
            )
        finally:
            db.close()

        all_assignee_ids = set()
        all_project_ids = set()
        for t in rows:
            if t.assignee_id:
                all_assignee_ids.add(t.assignee_id)
            if t.project_id:
                all_project_ids.add(t.project_id)

        assignee_cache = {}
        if all_assignee_ids:
            assignee_cache = self._batch_get_user_info(
                [UUID4(str(x)) for x in all_assignee_ids]
            )
        project_cache = {}
        if all_project_ids:
            project_cache = self._batch_get_project_info(
                [UUID4(str(x)) for x in all_project_ids]
            )

        tasks = [
            TaskResponse(
                id=UUID4(str(t.id)),
                title=t.title,
                content=t.content,
                status=self._coerce_task_status(t.status),
                due_date=t.due_date,
                assignee=assignee_cache.get(str(t.assignee_id))
                if t.assignee_id
                else None,
                project=project_cache.get(str(t.project_id))
                if t.project_id
                else None,
            )
            for t in rows
        ]

        return TasksPaginatedResponse(
            tasks=tasks,
            total=int(total_count),
            offset=off,
            limit=lim,
        )
    
    def get_task_attachments(
        self,
        task_id: UUID4,
        user_id: Optional[UUID4] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[TaskGetAttachmentResponse]:
        _ = user_id
        tid = UUID(str(task_id))
        db = SyncSessionLocal()
        try:
            stmt = (
                select(Attachment)
                .where(
                    Attachment.entity_type == AttachmentType.TASk.value,
                    Attachment.entity_id == tid,
                )
                .order_by(Attachment.created_at.desc())
            )
            if offset:
                stmt = stmt.offset(offset)
            if limit:
                stmt = stmt.limit(limit)
            rows = list(db.scalars(stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task attachments: {e}",
            )
        finally:
            db.close()

        attachments = []
        for att in rows:
            file_data = self.files_service.get_file_with_url(UUID4(str(att.file_id)))
            ts = att.created_at
            attachments.append(
                TaskGetAttachmentResponse(
                    id=UUID4(str(att.id)),
                    file_id=UUID4(str(att.file_id)),
                    file_name=file_data["file"]["name"],
                    task_id=task_id,
                    created_at=ts,
                    updated_at=ts,
                )
            )
        return attachments
    
    def get_task_attachment_with_url(
        self,
        attachment_id: UUID4,
    ) -> TaskGetAttachmentWithUrlResponse:
        aid = UUID(str(attachment_id))
        db = SyncSessionLocal()
        try:
            row = db.get(Attachment, aid)
        finally:
            db.close()

        if (
            not row
            or row.entity_type != AttachmentType.TASk.value
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task attachment not found",
            )

        file_data = self.files_service.get_file_with_url(UUID4(str(row.file_id)))
        file_url = self.files_service.get_file_url(UUID4(str(row.file_id)))
        ts = row.created_at
        return TaskGetAttachmentWithUrlResponse(
            id=UUID4(str(row.id)),
            file_id=UUID4(str(row.file_id)),
            file_name=file_data["file"]["name"],
            task_id=UUID4(str(row.entity_id)),
            created_at=ts,
            updated_at=ts,
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
        pid = UUID(str(comment_id))
        db = SyncSessionLocal()
        try:
            rows = list(
                db.scalars(
                    select(TaskComment)
                    .where(TaskComment.parent_id == pid)
                    .order_by(TaskComment.created_at.asc())
                ).all()
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get comment replies: {e}",
            )
        finally:
            db.close()

        if not rows:
            return []

        replies = []
        for reply in rows:
            rid = str(reply.id)
            cb = reply.created_by
            user_id_str = str(cb) if cb else ""
            user_info = user_info_cache.get(user_id_str) if user_id_str else None
            if not user_info and cb:
                user_info = self._get_user_info(UUID4(str(cb)))
                user_info_cache[user_id_str] = user_info

            subreplies = self._get_task_comment_replies(
                UUID4(str(reply.id)), user_timezone, user_info_cache
            )
            attachments = self._batch_get_attachments([rid])

            replies.append(
                TaskGetCommentResponse(
                    id=UUID4(str(reply.id)),
                    content=reply.content,
                    comment_by=user_info,
                    message_time=calculate_time_ago(reply.created_at, user_timezone),
                    attachments=attachments.get(rid, []),
                    replies=subreplies,
                )
            )
        
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
        tid = UUID(str(task_id))
        db = SyncSessionLocal()
        try:
            filt = (TaskComment.task_id == tid) & (TaskComment.parent_id.is_(None))
            total_count = (
                db.scalar(
                    select(func.count()).select_from(TaskComment).where(filt)
                )
                or 0
            )
            stmt = (
                select(TaskComment)
                .where(filt)
                .order_by(TaskComment.created_at.desc())
            )
            lim, off, page_stmt = apply_sa_limit_offset(stmt, limit, offset)
            top_rows = list(db.scalars(page_stmt).all())
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task comments: {e}",
            )
        finally:
            db.close()

        if not top_rows:
            return [], int(total_count)

        top_data = [
            {
                "id": str(c.id),
                "content": c.content,
                "created_by": str(c.created_by) if c.created_by else None,
                "created_at": c.created_at,
                "parent_id": str(c.parent_id) if c.parent_id else None,
            }
            for c in top_rows
        ]
        base_comment_ids = [c["id"] for c in top_data]

        all_user_ids = set()
        for comment in top_data:
            if comment.get("created_by"):
                all_user_ids.add(comment["created_by"])

        comments_by_parent = self._get_all_comments_for_task(task_id, base_comment_ids)

        for comments in comments_by_parent.values():
            for comment in comments:
                if comment.get("created_by"):
                    all_user_ids.add(comment["created_by"])

        all_comment_ids = []
        for comments in comments_by_parent.values():
            all_comment_ids.extend([str(c["id"]) for c in comments])
        all_comment_ids.extend(base_comment_ids)

        user_info_cache = self._batch_get_user_info(
            [UUID4(uid) if isinstance(uid, str) else uid for uid in all_user_ids]
        )
        attachments_by_comment = self._batch_get_attachments(all_comment_ids)

        comments = []
        for comment in top_data:
            comments.append(
                self._build_comment_tree(
                    comment,
                    comments_by_parent,
                    attachments_by_comment,
                    user_info_cache,
                    user_timezone,
                )
            )

        return comments, int(total_count)
    
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

        tid = UUID(str(task_id))
        db = SyncSessionLocal()
        try:
            rows = list(
                db.scalars(
                    select(TaskComment)
                    .where(
                        TaskComment.task_id == tid,
                        TaskComment.parent_id.isnot(None),
                    )
                    .order_by(TaskComment.created_at.asc())
                ).all()
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get all comments for task: {e}",
            )
        finally:
            db.close()

        comments_by_parent: Dict[str, List[Dict]] = {}
        for c in rows:
            parent_id = str(c.parent_id) if c.parent_id else ""
            if parent_id not in comments_by_parent:
                comments_by_parent[parent_id] = []
            comments_by_parent[parent_id].append(
                {
                    "id": str(c.id),
                    "content": c.content,
                    "created_by": str(c.created_by) if c.created_by else None,
                    "created_at": c.created_at,
                    "parent_id": parent_id,
                }
            )

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
        
        uuids = [UUID(cid) for cid in comment_ids]
        db = SyncSessionLocal()
        try:
            rows = db.execute(
                select(Attachment, File.name, File.size_bytes, File.content_type)
                .join(File, File.id == Attachment.file_id)
                .where(
                    Attachment.entity_type == AttachmentType.COMMENT.value,
                    Attachment.entity_id.in_(uuids),
                )
            ).all()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to batch get attachments: {e}",
            )
        finally:
            db.close()

        attachments_by_comment: Dict[str, List[AttachmentResponse]] = {}
        for attachment, fname, fsize, fctype in rows:
            comment_id = str(attachment.entity_id)
            if comment_id not in attachments_by_comment:
                attachments_by_comment[comment_id] = []
            attachments_by_comment[comment_id].append(
                AttachmentResponse(
                    id=UUID4(str(attachment.id)),
                    file_id=UUID4(str(attachment.file_id)),
                    file_name=fname or "",
                    file_size=calculate_file_size(fsize or 0),
                    content_type=fctype or "",
                )
            )

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
        
        # Get replies for this comment and sort chronologically
        replies_data = comments_by_parent.get(comment_id, [])
        # Sort replies by created_at to ensure chronological order
        replies_data = sorted(replies_data, key=lambda x: x.get('created_at', ''))
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
        tid = UUID(str(task_id))
        db = SyncSessionLocal()
        try:
            rows = list(
                db.scalars(
                    select(Task).where(Task.parent_id == tid)
                ).all()
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get subtasks: {e}",
            )
        finally:
            db.close()

        if not rows:
            return []

        return [
            TaskBaseResponse(
                id=UUID4(str(t.id)),
                title=t.title,
                content=t.content,
                status=self._coerce_task_status(t.status),
                due_date=t.due_date,
                assignee_id=UUID4(str(t.assignee_id)) if t.assignee_id else None,
                project_id=UUID4(str(t.project_id)),
            )
            for t in rows
        ]
    
    def _get_user_info(
        self,
        user_id: UUID4,
    ) -> TaskUserInfoResponse:
        uid = UUID(str(user_id))
        db = SyncSessionLocal()
        try:
            row = db.execute(
                select(
                    Profile.user_id,
                    Profile.display_name,
                    Profile.avatar_file_id,
                ).where(Profile.user_id == uid)
            ).one_or_none()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user info: {e}",
            )
        finally:
            db.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        p_uid, display_name, avatar_file_id = row
        avatar_url = None
        if avatar_file_id:
            try:
                avatar_url = self.files_service.get_file_url(UUID4(str(avatar_file_id)))
            except Exception:
                pass

        return TaskUserInfoResponse(
            id=UUID4(str(p_uid)),
            display_name=display_name,
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
        
        user_id_strings = [str(uid) if isinstance(uid, UUID) else uid for uid in user_ids]
        uuids = [UUID(x) for x in user_id_strings]
        db = SyncSessionLocal()
        try:
            rows = db.execute(
                select(
                    Profile.user_id,
                    Profile.display_name,
                    Profile.avatar_file_id,
                ).where(Profile.user_id.in_(uuids))
            ).all()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to batch get user info: {e}",
            )
        finally:
            db.close()

        user_info_dict = {}
        for p_uid, display_name, avatar_file_id in rows:
            avatar_url = None
            if avatar_file_id:
                try:
                    avatar_url = self.files_service.get_file_url(UUID4(str(avatar_file_id)))
                except Exception:
                    pass
            user_info_dict[str(p_uid)] = TaskUserInfoResponse(
                id=UUID4(str(p_uid)),
                display_name=display_name,
                avatar_url=avatar_url,
            )

        return user_info_dict
    
    def _batch_get_project_info(
        self,
        project_ids: List[UUID4],
    ) -> Dict[str, 'TaskProjectInfo']:
        """
        Batch fetch project info for multiple project IDs with Redis caching.
        Returns a dictionary mapping project_id to TaskProjectInfo.
        """
        from app.schemas.tasks import TaskProjectInfo
        
        if not project_ids:
            return {}
        
        CACHE_TTL = 300  # 5 minutes cache
        project_info_dict = {}
        uncached_project_ids = []
        
        # Check cache for each project using CacheService
        for project_id in project_ids:
            project_id_str = str(project_id)
            cache_key = f"project_info:{project_id_str}"
            cached = cache_service.get(cache_key)
            if cached:
                try:
                    # Convert id string to UUID4 for TaskProjectInfo
                    cached['id'] = UUID4(cached['id'])
                    project_info_dict[project_id_str] = TaskProjectInfo(**cached)
                except Exception as e:
                    logger.warning(f"Error parsing cached project data: {e}")
                    uncached_project_ids.append(project_id)
            else:
                uncached_project_ids.append(project_id)
        
        # Fetch uncached projects from database
        if uncached_project_ids:
            project_id_strings = [
                str(pid) if isinstance(pid, UUID) else pid for pid in uncached_project_ids
            ]
            puuids = [UUID(x) for x in project_id_strings]
            db = SyncSessionLocal()
            try:
                rows = db.execute(
                    select(
                        Project.id,
                        Project.name,
                        Project.avatar_color,
                        Project.avatar_icon,
                        Project.avatar_file_id,
                    ).where(Project.id.in_(puuids))
                ).all()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to batch get project info: {e}",
                )
            finally:
                db.close()

            for pid, name, avc, avi, avf in rows:
                project_id_str = str(pid)
                avatar_url = None
                if avf:
                    try:
                        avatar_url = self.files_service.get_file_url(UUID4(str(avf)))
                    except Exception:
                        pass

                project_info = TaskProjectInfo(
                    id=UUID4(str(pid)),
                    name=name,
                    avatar_color=avc,
                    avatar_icon=avi,
                    avatar_url=avatar_url,
                )

                project_info_dict[project_id_str] = project_info

                cache_data = {
                    "id": project_id_str,
                    "name": name,
                    "avatar_color": avc,
                    "avatar_icon": avi,
                    "avatar_url": avatar_url,
                }
                cache_service.set(
                    f"project_info:{project_id_str}", cache_data, ttl=CACHE_TTL
                )
        
        return project_info_dict
    
    def _get_user_timezone(
        self,
        user_id: UUID4,
    ) -> str:
        uid = UUID(str(user_id))
        db = SyncSessionLocal()
        try:
            tz = db.scalar(select(Profile.timezone).where(Profile.user_id == uid))
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user timezone: {e}",
            )
        finally:
            db.close()

        if not tz:
            return "utc"
        return str(tz)
    
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
        if table_name != "tasks":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported table for parent resolution",
            )
        eid = UUID(str(entity_id))
        db = SyncSessionLocal()
        try:
            pid = db.scalar(select(Task.parent_id).where(Task.id == eid))
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get parent id: {e}",
            )
        finally:
            db.close()

        if not pid:
            return None
        return UUID4(str(pid))
    
    def get_task_depth_info(
        self,
        task_id: UUID4,
        project_id: Optional[UUID4] = None,
    ) -> 'TaskDepthResponse':
        """
        Get the depth level of a task and determine if it's an innermost task.
        An innermost task is one that has reached the maximum allowed depth and cannot have subtasks.
        
        Args:
            task_id: The task ID to check
            project_id: Optional project ID to verify the task belongs to the project
        """
        from app.schemas.tasks import TaskDepthResponse
        
        tid = UUID(str(task_id))
        db = SyncSessionLocal()
        try:
            row = db.execute(
                select(Task.id, Task.parent_id, Task.project_id).where(Task.id == tid)
            ).one_or_none()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get task: {e}",
            )
        finally:
            db.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found",
            )
        _, _parent, t_proj = row
        if project_id and str(t_proj) != str(project_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Task does not belong to the specified project",
            )
        
        # Calculate depth by traversing up the parent chain
        depth = 0
        current_task_id = task_id
        
        while depth < settings.MAX_SUBTASK_DEPTH:
            parent_id = self._get_parent_id('tasks', current_task_id)
            if parent_id is None:
                # Reached the root (no parent), this is the current depth
                break
            depth += 1
            current_task_id = parent_id
        
        # Check if task is innermost (at maximum depth)
        is_innermost = depth >= settings.MAX_SUBTASK_DEPTH
        
        return TaskDepthResponse(
            task_id=task_id,
            depth_level=depth,
            is_innermost=is_innermost,
            max_allowed_depth=settings.MAX_SUBTASK_DEPTH,
        )
