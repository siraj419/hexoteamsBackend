from pydantic import BaseModel, UUID4
from datetime import datetime
from enum import Enum
from typing import List, Optional


class AttachmentType(Enum):
    TASk = 'task'
    PROJECT = 'project'
    COMMENT = 'comment'
    CHAT = 'chat'


class AttachmentRequest(BaseModel):
    file_id: UUID4
    
class AttachmentResponse(BaseModel):
    id: UUID4
    file_id: UUID4
    file_name: str
    file_size: str
    content_type: str

class AttachmentGetPaginatedResponse(BaseModel):
    attachments: List[AttachmentResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None