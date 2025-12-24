from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import Any, List, Optional
from pydantic import UUID4

from app.schemas.tasks import (
    TaskCreateRequest,
    TaskCreateResponse,
    TaskCommentUpdateRequest,
    TaskCommentUpdateResponse,
    TaskGetResponse,
    TaskResponse,
    TaskRequest,
    TaskLinkRequest,
    TaskCreateAttachmentRequest,
    TaskGetCommentResponse,
    TaskGetCommentsPaginatedResponse,
    TaskStatus,
    TaskChangeAssigneeRequest,
    TaskChangeStatusRequest,
    TaskUpdateDetailsRequest,
    TaskUserInfoResponse,
)
from app.services.task import (
    TaskService,
    TaskCommentCreateResponse,
    TaskCommentCreateRequest,
)
from app.schemas.tasks import (
    TaskLinkUpdateRequest,
)

from app.routers.deps import get_project_member, verify_task_delete_permission
from app.services.activity import ActivityService, ActivityType
from app.services.files import FilesService
from app.services.attachment import AttachmentService
from app.schemas.activities import ActivityGetPaginatedResponse
from app.schemas.chat import AttachmentDownloadResponse
from app.schemas.links import (
    LinkEntityType,
    LinkResponse,
    LinkGetPaginatedResponse,
)
from app.schemas.attachments import (
    AttachmentType,
    AttachmentRequest,
    AttachmentResponse,
    AttachmentGetPaginatedResponse,
)
from app.services.link import LinkService
from app.services.attachment import AttachmentService
from app.services.files import FilesService

router = APIRouter()

@router.post('/', response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    task_request: TaskCreateRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Create a new task for a project
    """
    task_service = TaskService()
    return task_service.create_task(task_request, member['user_id'], project_id=project_id)

@router.post('/{task_id}/subtasks', response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
def create_subtask(
    task_id: UUID4,
    task_request: TaskCreateRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Create a new subtask for a task
    """
    task_service = TaskService()
    return task_service.create_task(task_request, member['user_id'], parent_id=task_id, project_id=project_id)


@router.post('/{task_id}/comments', response_model=TaskCommentCreateResponse, status_code=status.HTTP_201_CREATED)
def add_task_comment(
    task_id: UUID4,
    task_request: TaskCommentCreateRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Add a comment to a task
    """
    task_service = TaskService()
    return task_service.add_task_comment(task_id, task_request, member['user_id'])

@router.post('/{task_id}/comments/{comment_id}/reply')
def reply_to_task_comment(
    task_id: UUID4,
    comment_id: UUID4,
    task_request: TaskCommentCreateRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Add a reply to a task comment
    """
    task_service = TaskService()
    return task_service.add_task_comment(task_id, task_request, member['user_id'], parent_id=comment_id)

@router.put('/comments/{comment_id}', response_model=TaskCommentUpdateResponse, status_code=status.HTTP_200_OK)
def update_task_comment(
    comment_id: UUID4,
    task_request: TaskCommentUpdateRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Update a task comment and optionally add attachments
    """
    task_service = TaskService()
    return task_service.update_task_comment(comment_id, task_request, member['user_id'])

@router.delete('/comments/{comment_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task_comment(
    comment_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Delete a task comment. Only the user who created the comment can delete it.
    """
    task_service = TaskService()
    task_service.delete_task_comment(comment_id, member['user_id'])
    return None

@router.post('/{task_id}/links', response_model=LinkResponse, status_code=status.HTTP_201_CREATED)
def add_task_link(
    task_id: UUID4,
    task_request: TaskLinkRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Add a link to a task
    """
    task_service = TaskService()
    user_timezone = task_service._get_user_timezone(member['user_id'])
    link_service = LinkService(user_timezone)
    return link_service.create_link(task_request, task_id, LinkEntityType.TASK)

@router.get('/{task_id}/links', response_model=LinkGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_task_links(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """
    Get all links for a task with pagination.
    Optimized to fetch all data in minimal queries.
    """
    task_service = TaskService()
    user_timezone = task_service._get_user_timezone(member['user_id'])
    link_service = LinkService(user_timezone)
    return link_service.get_links(task_id, LinkEntityType.TASK, limit=limit, offset=offset)

@router.put('/{task_id}/links/{link_id}', response_model=LinkResponse, status_code=status.HTTP_200_OK)
def update_task_link(
    task_id: UUID4,
    link_id: UUID4,
    task_request: TaskLinkUpdateRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Update a task link.
    Optimized single query operation.
    """
    task_service = TaskService()
    user_timezone = task_service._get_user_timezone(member['user_id'])
    link_service = LinkService(user_timezone)
    return link_service.update_link(link_id, task_request)

@router.delete('/{task_id}/links/{link_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task_link(
    link_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Delete a task link.
    Optimized single query operation.
    """
    link_service = LinkService()
    link_service.delete_link(link_id)
    return None

@router.post('/{task_id}/attachments', response_model=AttachmentResponse, status_code=status.HTTP_201_CREATED)
def add_task_attachment(
    task_id: UUID4,
    task_request: TaskCreateAttachmentRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Add an attachment to a task
    """
    
    attachment_service = AttachmentService(files_service=FilesService())
    return attachment_service.add_attachment(AttachmentType.TASk, task_id, task_request.file_id)

@router.get('/{task_id}/attachments', response_model=AttachmentGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_task_attachments(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """
    Get all attachments for a task with pagination.
    Optimized to fetch all data in minimal queries.
    """
    attachment_service = AttachmentService(files_service=FilesService())
    return attachment_service.get_attachments(AttachmentType.TASk, task_id, limit=limit, offset=offset)

@router.delete('/attachments/{attachment_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task_attachment(
    attachment_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Delete a task attachment.
    Optimized single query operation.
    """
    attachment_service = AttachmentService(files_service=FilesService())
    attachment_service.delete_attachment(attachment_id)
    return None

@router.get('/{task_id}/get', response_model=TaskGetResponse, status_code=status.HTTP_200_OK)
def get_task(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Get a task by its ID
    """
    task_service = TaskService()
    return task_service.get_task(member['user_id'], task_id)

@router.get('/{task_id}/assignee', response_model=TaskUserInfoResponse, status_code=status.HTTP_200_OK)
def get_task_assignee(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Get the assignee user info for a task.
    Returns 404 if task has no assignee.
    Optimized single query operation.
    """
    task_service = TaskService()
    assignee = task_service.get_task_assignee(task_id)
    if not assignee:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task has no assignee"
        )
    return assignee

@router.patch('/{task_id}/assignee', response_model=TaskResponse, status_code=status.HTTP_200_OK)
def change_task_assignee(
    task_id: UUID4,
    task_request: TaskChangeAssigneeRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Change, assign, or unassign the assignee of a task.
    - To assign: provide assignee_id
    - To unassign: provide None or omit assignee_id
    - To change: provide different assignee_id
    Task can only have exactly one assignee (or none).
    Optimized single query operation.
    Records activity when assignee changes.
    """
    task_service = TaskService()
    return task_service.change_task_assignee(task_id, task_request, member['user_id'])

@router.patch('/{task_id}/status', response_model=TaskResponse, status_code=status.HTTP_200_OK)
def change_task_status(
    task_id: UUID4,
    task_request: TaskChangeStatusRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Change the status of a task.
    Records activity when status changes.
    Optimized single query operation.
    """
    task_service = TaskService()
    return task_service.change_task_status(task_id, task_request, member['user_id'])

@router.patch('/{task_id}', response_model=TaskResponse, status_code=status.HTTP_200_OK)
def update_task_details(
    task_id: UUID4,
    task_request: TaskUpdateDetailsRequest,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Update task details (title, content, and/or due_date).
    At least one of title, content, or due_date must be provided.
    Optimized single query operation.
    """
    task_service = TaskService()
    return task_service.update_task_details(task_id, task_request, member['user_id'])

@router.get('/{task_id}/activities', response_model=ActivityGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_task_activities(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """
    Get paginated task activities.
    Returns activities with user info and optimized file URL fetching.
    Supports pagination with limit and offset.
    """
    activity_service = ActivityService(files_service=FilesService())
    return activity_service.get_activities_paginated(
        task_id,
        ActivityType.TASK,
        limit=limit,
        offset=offset,
    )

@router.get('/{task_id}/comments', response_model=TaskGetCommentsPaginatedResponse, status_code=status.HTTP_200_OK)
def get_task_comments(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """
    Get all base comments for a task with their replies and attachments.
    Only returns top-level comments (not replies), with replies nested under their parents.
    Supports pagination with optional limit and offset parameters.
    Returns paginated response with total count.
    """
    task_service = TaskService()
    return task_service.get_task_comments(task_id, member['user_id'], limit=limit, offset=offset)

@router.get('/', response_model=List[TaskResponse], status_code=status.HTTP_200_OK)
def list_tasks(
    project_id: UUID4 = Query(...),
    created_by: Optional[UUID4] = None,
    member: Any = Depends(get_project_member),
    search: Optional[str] = None,
    assignee_id: Optional[UUID4] = None,
    status: Optional[TaskStatus] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """
    List all tasks for a project
    """
    task_service = TaskService()
    return task_service.list_tasks(
        project_id,
        user_id=created_by,
        search=search,
        assignee_id=assignee_id,
        status=status,
        limit=limit,
        offset=offset,
    )

@router.get('/{task_id}/subtasks', response_model=List[TaskResponse], status_code=status.HTTP_200_OK)
def list_subtasks(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
    search: Optional[str] = None,
    assignee_id: Optional[UUID4] = None,
    status: Optional[TaskStatus] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """
    List all subtasks for a task.
    Returns subtasks in the same format as list tasks endpoint.
    Supports filtering and pagination.
    """
    task_service = TaskService()
    return task_service.list_subtasks(
        task_id,
        search=search,
        user_id=member['user_id'],
        assignee_id=assignee_id,
        status=status,
        limit=limit,
        offset=offset,
    )

@router.delete('/{task_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: UUID4,
    permission: dict = Depends(verify_task_delete_permission),
):
    """
    Delete a task.
    
    Only the following users can delete a task:
    - The user who created the task
    - Organization owners
    - Organization admins
    
    Requires project_id as query parameter to verify project membership.
    
    Query params:
        - project_id: UUID4 (required) - The project ID to verify membership
    
    Example:
        DELETE /api/v1/tasks/{task_id}?project_id={project_id}
    """
    task_service = TaskService()
    task_service.delete_task(
        task_id=UUID4(permission['task_id']),
        user_id=UUID4(permission['user_id']),
        force_delete=permission.get('is_org_admin', False)
    )
    return None

@router.get('/attachments/{attachment_id}/download', response_model=AttachmentDownloadResponse, status_code=status.HTTP_200_OK)
def download_task_attachment(
    attachment_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Download a task attachment.
    
    Only project members can download task attachments.
    The endpoint verifies:
    1. Attachment exists and is a task attachment
    2. Task belongs to the specified project
    3. User is a member of the project
    
    Query params:
        - project_id: UUID4 (required) - The project ID to verify membership
    
    Returns:
        AttachmentDownloadResponse with download_url and expires_at
    
    Example:
        GET /api/v1/tasks/attachments/{attachment_id}/download?project_id={project_id}
    """
    files_service = FilesService()
    attachment_service = AttachmentService(files_service=files_service)
    
    try:
        result = attachment_service.get_task_attachment_download_url(
            attachment_id=attachment_id,
            user_id=UUID4(member['user_id'])
        )
        return AttachmentDownloadResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get download URL: {str(e)}"
        )

@router.get('/comments/attachments/{attachment_id}/download', response_model=AttachmentDownloadResponse, status_code=status.HTTP_200_OK)
def download_comment_attachment(
    attachment_id: UUID4,
    project_id: UUID4 = Query(...),
    member: Any = Depends(get_project_member),
):
    """
    Download a comment attachment.
    
    Only project members can download comment attachments.
    The endpoint verifies:
    1. Attachment exists and is a comment attachment
    2. Comment belongs to a task in the specified project
    3. User is a member of the project
    
    Query params:
        - project_id: UUID4 (required) - The project ID to verify membership
    
    Returns:
        AttachmentDownloadResponse with download_url and expires_at
    
    Example:
        GET /api/v1/tasks/comments/attachments/{attachment_id}/download?project_id={project_id}
    """
    files_service = FilesService()
    attachment_service = AttachmentService(files_service=files_service)
    
    try:
        result = attachment_service.get_comment_attachment_download_url(
            attachment_id=attachment_id,
            user_id=UUID4(member['user_id'])
        )
        return AttachmentDownloadResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get download URL: {str(e)}"
        )