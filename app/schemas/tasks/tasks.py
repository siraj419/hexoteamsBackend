from pydantic import BaseModel, UUID4, Field
from typing import Optional, List
from datetime import datetime

from enum import Enum

from app.schemas.activities import ActivityResponse
from app.schemas.links import LinkResponse, LinkRequest, LinkUpdateRequest

from app.schemas.attachments import AttachmentResponse

class TaskStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"


class TaskRequest(BaseModel):
    pass

class TaskCreateRequest(BaseModel):
    title: str
    content: Optional[str] = None
    status: TaskStatus
    due_date: Optional[datetime] = None
    assignee_id: Optional[UUID4] = None
    file_ids: Optional[List[UUID4]] = []


class TaskBaseResponse(BaseModel):
    id: UUID4
    title: str
    parent_id: Optional[UUID4] = None
    content: Optional[str] = None
    status: TaskStatus
    due_date: Optional[datetime] = None
    assignee_id: Optional[UUID4] = None
    project_id: UUID4

class TaskCreateResponse(TaskBaseResponse):
    pass

class TaskUpdateResponse(TaskBaseResponse):
    pass

class TaskUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    status: Optional[TaskStatus] = None
    due_date: Optional[datetime] = None
    assignee_id: Optional[UUID4] = None

class TaskChangeAssigneeRequest(BaseModel):
    assignee_id: Optional[UUID4] = None

class TaskChangeStatusRequest(BaseModel):
    status: TaskStatus

class TaskUpdateDetailsRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    due_date: Optional[datetime] = None

class TaskAttachmentResponse(BaseModel):
    id: UUID4
    file_id: UUID4
    file_name: str
    task_id: UUID4
    created_at: datetime
    updated_at: datetime

class TaskCreateAttachmentResponse(TaskAttachmentResponse):
    pass

class TaskGetAttachmentResponse(TaskAttachmentResponse):
    pass

class TaskGetAttachmentWithUrlResponse(TaskGetAttachmentResponse):
    file_url: str

class TaskCommentAttachmentResponse(BaseModel):
    id: UUID4
    file_id: UUID4
    file_name: str
    size_bytes: int
    content_type: str

class TaskCommentCreateAttachmentResponse(TaskCommentAttachmentResponse):
    pass
    
class TaskCommentResponse(BaseModel):
    id: UUID4
    parent_id: Optional[UUID4] = None
    content: str

class TaskCommentCreateRequest(BaseModel):
    content: str
    file_ids: Optional[List[UUID4]] = []

class TaskCommentUpdateRequest(BaseModel):
    content: Optional[str] = None
    file_ids: Optional[List[UUID4]] = []

class TaskCommentCreateResponse(TaskCommentResponse):
    message_time: str
    attachments: Optional[List[AttachmentResponse]] = []

class TaskCommentUpdateResponse(TaskCommentResponse):
    message_time: str
    attachments: Optional[List[AttachmentResponse]] = []

class TaskUserInfoResponse(BaseModel):
    id: UUID4
    display_name: str
    avatar_url: Optional[str] = None

class TaskGetCommentResponse(TaskCommentResponse):
    comment_by: TaskUserInfoResponse
    message_time: str
    attachments: Optional[List[AttachmentResponse]] = []
    replies: Optional[List['TaskGetCommentResponse']] = []

class TaskGetCommentsPaginatedResponse(BaseModel):
    comments: List[TaskGetCommentResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None

# TaskProjectInfo and TaskResponse must be defined before classes that
# use them as forward references (PaginatedSubtasks, TaskSubtasksPaginatedResponse, etc.)

class TaskProjectInfo(BaseModel):
    id: UUID4
    name: str
    avatar_color: Optional[str] = None
    avatar_icon: Optional[str] = None
    avatar_url: Optional[str] = None

class TaskResponse(BaseModel):
    id: UUID4
    title: str
    content: Optional[str] = None
    status: TaskStatus
    due_date: Optional[datetime] = None
    assignee: Optional[TaskUserInfoResponse] = None
    project: Optional[TaskProjectInfo] = None

class TaskSubtasksPaginatedResponse(BaseModel):
    subtasks: List[TaskResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None

class TasksPaginatedResponse(BaseModel):
    tasks: List[TaskResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None

class PaginatedAttachments(BaseModel):
    attachments: List[AttachmentResponse] = []
    total: int = 0
    offset: Optional[int] = None
    limit: Optional[int] = None

class PaginatedLinks(BaseModel):
    links: List[LinkResponse] = []
    total: int = 0
    offset: Optional[int] = None
    limit: Optional[int] = None

class PaginatedSubtasks(BaseModel):
    subtasks: List[TaskResponse] = []
    total: int = 0
    offset: Optional[int] = None
    limit: Optional[int] = None

class TaskGetResponse(BaseModel):
    id: UUID4
    title: str
    content: Optional[str] = None
    status: TaskStatus
    due_date: Optional[datetime] = None
    assignee: Optional[TaskUserInfoResponse] = None
    project_id: UUID4
    comments: Optional[List['TaskGetCommentResponse']] = []
    attachments_paginated: PaginatedAttachments = PaginatedAttachments()
    activities: Optional[List[ActivityResponse]] = []
    links_paginated: PaginatedLinks = PaginatedLinks()
    sub_tasks_paginated: PaginatedSubtasks = PaginatedSubtasks()

class TaskLinkRequest(LinkRequest):
    pass

class TaskCreateAttachmentRequest(BaseModel):
    file_id: UUID4

class TaskLinkUpdateRequest(LinkUpdateRequest):
    pass

class TaskMinimalResponse(BaseModel):
    id: UUID4
    title: str

class ProjectTasksMinimalResponse(BaseModel):
    tasks: List[TaskMinimalResponse]

class TaskDepthResponse(BaseModel):
    task_id: UUID4
    depth_level: int
    is_innermost: bool
    max_allowed_depth: int

# Rebuild models that use forward references
TaskGetCommentResponse.model_rebuild()
TaskGetResponse.model_rebuild()
