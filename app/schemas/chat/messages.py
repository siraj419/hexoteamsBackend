from pydantic import BaseModel, UUID4, Field, field_validator
from typing import Optional, List, Any
from datetime import datetime
from enum import Enum


class MessageType(str, Enum):
    TEXT = "text"
    FILE = "file"
    SYSTEM = "system"


class ChatType(str, Enum):
    PROJECT = "project"
    DIRECT = "direct"


class ProjectMessageCreate(BaseModel):
    body: Optional[str] = Field(None, max_length=10000, description="Message body text")
    attachments: Optional[List[UUID4]] = Field(None, max_items=5, description="List of attachment IDs")
    reply_to_id: Optional[UUID4] = Field(None, description="ID of message being replied to")
    
    @field_validator('body', 'attachments')
    @classmethod
    def validate_content(cls, v, info):
        body = info.data.get('body') if info.field_name == 'attachments' else v
        attachments = v if info.field_name == 'attachments' else info.data.get('attachments')
        
        if not body and not attachments:
            raise ValueError('Message must have either body text or attachments')
        return v


class ProjectMessageUpdate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000, description="Updated message body")


class ProjectMessageResponse(BaseModel):
    id: UUID4
    project_id: UUID4
    user_id: UUID4
    body: Optional[str]
    attachments: Optional[Any]
    message_type: MessageType
    reply_to_id: Optional[UUID4]
    read_by: List[UUID4] = Field(default_factory=list)
    created_at: datetime
    edited_at: Optional[datetime]
    deleted_at: Optional[datetime]
    
    user: Optional[dict] = None
    reply_to: Optional[dict] = None

    @field_validator('read_by', mode='before')
    @classmethod
    def normalize_read_by(cls, v):
        """Normalize read_by field from various formats to list of UUIDs"""
        import json
        
        # Handle None
        if v is None:
            return []
        
        # If already a list, validate and convert
        if isinstance(v, list):
            result = []
            for item in v:
                if item is None:
                    continue
                item_str = str(item).strip()
                # If item is a JSON string, parse it
                if item_str.startswith('[') and item_str.endswith(']'):
                    try:
                        parsed = json.loads(item_str)
                        if isinstance(parsed, list):
                            result.extend([str(x).strip() for x in parsed if x])
                        else:
                            result.append(str(parsed).strip())
                    except (json.JSONDecodeError, TypeError):
                        result.append(item_str)
                elif item_str:
                    result.append(item_str)
            return result
        
        # Handle string - parse JSON
        if isinstance(v, str):
            v_stripped = v.strip()
            if not v_stripped or v_stripped == '[]':
                return []
            try:
                parsed = json.loads(v_stripped)
                # Recursively process if needed
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if x]
                elif isinstance(parsed, str):
                    # Double-encoded, parse again
                    try:
                        parsed2 = json.loads(parsed)
                        if isinstance(parsed2, list):
                            return [str(x).strip() for x in parsed2 if x]
                    except (json.JSONDecodeError, TypeError):
                        pass
                return []
            except (json.JSONDecodeError, TypeError):
                return []
        
        return []

    class Config:
        from_attributes = True


class DirectMessageCreate(BaseModel):
    body: Optional[str] = Field(None, max_length=10000, description="Message body text")
    attachments: Optional[List[UUID4]] = Field(None, max_items=5, description="List of attachment IDs")
    
    @field_validator('body', 'attachments')
    @classmethod
    def validate_content(cls, v, info):
        body = info.data.get('body') if info.field_name == 'attachments' else v
        attachments = v if info.field_name == 'attachments' else info.data.get('attachments')
        
        if not body and not attachments:
            raise ValueError('Message must have either body text or attachments')
        return v


class DirectMessageUpdate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000, description="Updated message body")


class DirectMessageResponse(BaseModel):
    id: UUID4
    sender_id: UUID4
    receiver_id: UUID4
    organization_id: UUID4
    body: Optional[str]
    attachments: Optional[Any]
    message_type: MessageType
    created_at: datetime
    edited_at: Optional[datetime]
    deleted_at: Optional[datetime]
    read_at: Optional[datetime]
    
    sender: Optional[dict] = None
    receiver: Optional[dict] = None

    class Config:
        from_attributes = True


class DirectMessageListResponse(BaseModel):
    messages: List[DirectMessageResponse]
    total: int
    limit: Optional[int] = None
    offset: Optional[int] = None


class MessageReadRequest(BaseModel):
    last_read_message_id: UUID4


class TypingIndicatorRequest(BaseModel):
    is_typing: bool


class AttachmentResponse(BaseModel):
    id: UUID4
    message_id: UUID4
    message_type: str
    file_name: str
    file_size: int
    file_type: str
    storage_path: str
    thumbnail_path: Optional[str]
    uploaded_by: UUID4
    created_at: datetime

    class Config:
        from_attributes = True


class AttachmentUploadResponse(BaseModel):
    attachment_id: UUID4
    file_name: str
    file_size: str
    file_type: str
    thumbnail_url: Optional[str] = None


class AttachmentDownloadResponse(BaseModel):
    download_url: str
    expires_at: datetime


class ChatAttachmentDetailsResponse(BaseModel):
    attachment_id: UUID4
    file_name: str
    file_size: str
    file_type: str
    thumbnail_url: Optional[str] = None


class SearchResultResponse(BaseModel):
    message_id: UUID4
    chat_type: ChatType
    reference_id: UUID4
    body: Optional[str]
    user_id: UUID4
    user: Optional[dict]
    created_at: datetime
    relevance_score: float

    class Config:
        from_attributes = True

