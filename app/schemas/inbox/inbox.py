from pydantic import UUID4, BaseModel, Field
from datetime import datetime
from typing import List, Optional
from enum import Enum


class InboxEventType(str, Enum):
    ORGANIZATION_INVITATION = "organization_invitation"
    TASK_ASSIGNED = "task_assigned"
    TASK_UNASSIGNED = "task_unassigned"
    DIRECT_MESSAGE = "direct_message"
    TASK_COMPLETED = "task_completed"


class InboxResponse(BaseModel):
    id: UUID4
    title: str
    message: str
    message_time: str
    is_read: bool
    is_archived: Optional[bool] = False
    event_type: Optional[InboxEventType] = None
    reference_id: Optional[UUID4] = None


class InboxGetResponse(InboxResponse):
    pass


class InboxGetPaginatedResponse(BaseModel):
    inbox: List[InboxResponse]
    total: int
    offset: int
    limit: int


class InboxMarkReadRequest(BaseModel):
    inbox_id: UUID4


class InboxMarkReadResponse(BaseModel):
    success: bool
    message: str


class InboxArchiveRequest(BaseModel):
    inbox_id: UUID4


class InboxArchiveResponse(BaseModel):
    success: bool
    message: str


class InboxDeleteRequest(BaseModel):
    inbox_id: UUID4


class InboxDeleteResponse(BaseModel):
    success: bool
    message: str


class InboxUnarchiveRequest(BaseModel):
    inbox_id: UUID4


class InboxUnarchiveResponse(BaseModel):
    success: bool
    message: str


class InboxCreateRequest(BaseModel):
    title: str
    message: str
    user_id: UUID4
    org_id: UUID4
    user_by: UUID4
    event_type: Optional[InboxEventType] = None
    reference_id: Optional[UUID4] = None