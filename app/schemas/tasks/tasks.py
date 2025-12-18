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
    project_id: UUID4

class TaskCreateRequest(TaskRequest):
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

class TaskChangeAssigneeRequest(TaskRequest):
    assignee_id: Optional[UUID4] = None

class TaskChangeStatusRequest(TaskRequest):
    status: TaskStatus

class TaskUpdateDetailsRequest(TaskRequest):
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

class TaskCommentCreateRequest(TaskRequest):
    content: str
    file_ids: Optional[List[UUID4]] = []

class TaskCommentUpdateRequest(TaskRequest):
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

class TaskGetResponse(BaseModel):
    id: UUID4
    title: str
    content: Optional[str] = None
    status: TaskStatus
    due_date: Optional[datetime] = None
    assignee: Optional[TaskUserInfoResponse] = None
    project_id: UUID4
    comments: Optional[List['TaskGetCommentResponse']] = []
    attachments: Optional[List[AttachmentResponse]] = []
    activities: Optional[List[ActivityResponse]] = []
    links: Optional[List[LinkResponse]] = []
    sub_tasks: Optional[List[TaskBaseResponse]] = []

class TaskResponse(BaseModel):
    id: UUID4
    title: str
    content: Optional[str] = None
    status: TaskStatus
    due_date: Optional[datetime] = None
    assignee: Optional[TaskUserInfoResponse] = None
    
class TaskLinkRequest(TaskRequest, LinkRequest):
    pass

class TaskCreateAttachmentRequest(TaskRequest):
    file_id: UUID4

class TaskLinkUpdateRequest(TaskRequest, LinkUpdateRequest):
    pass