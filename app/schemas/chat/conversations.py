from pydantic import BaseModel, UUID4, Field, ConfigDict
from typing import Optional, List
from datetime import datetime


class WorkspaceUser(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID4
    email: Optional[str] = None
    full_name: Optional[str] = Field(None, serialization_alias="fullName")
    avatar_url: Optional[str] = Field(None, serialization_alias="avatarUrl")


class ConversationCreate(BaseModel):
    receiver_id: UUID4

class ConversationResponse(BaseModel):
    id: UUID4
    user1_id: UUID4
    user2_id: UUID4
    organization_id: UUID4
    last_message_at: datetime
    last_message_preview: Optional[str]
    created_at: datetime
    
    other_user: Optional[dict] = None
    unread_count: int = 0

    class Config:
        from_attributes = True


class ConversationListResponse(BaseModel):
    conversations: List[ConversationResponse]
    total: int
    limit: Optional[int] = None
    offset: Optional[int] = None


class UnreadCountResponse(BaseModel):
    chat_type: str
    reference_id: UUID4
    reference_name: str
    unread_count: int
    last_message_preview: Optional[str]
    last_message_at: Optional[datetime]


class NotificationSummaryResponse(BaseModel):
    total_unread: int
    project_chats: List[UnreadCountResponse]
    direct_messages: List[UnreadCountResponse]


class ProjectConversationResponse(BaseModel):
    project_id: UUID4
    project_name: str
    avatar_color: Optional[str] = None
    avatar_icon: Optional[str] = None
    avatar_url: Optional[str] = None
    last_message_at: Optional[datetime] = None
    last_message_preview: Optional[str] = None
    unread_count: int = 0


class ProjectConversationListResponse(BaseModel):
    conversations: List[ProjectConversationResponse]
    total: int
    limit: Optional[int] = None
    offset: Optional[int] = None


