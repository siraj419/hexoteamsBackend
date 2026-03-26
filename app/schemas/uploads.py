from typing import Dict
from uuid import UUID

from pydantic import BaseModel, Field


class PresignedUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)
    content_type: str = Field(..., min_length=1, max_length=255)


class PresignedUploadResponse(BaseModel):
    url: str
    fields: Dict[str, str]
    file_id: UUID
    s3_key: str
    expires_in_seconds: int = Field(default=300, description="POST policy TTL (fixed at 5 minutes)")
    max_file_size_bytes: int


class UploadConfirmRequest(BaseModel):
    file_id: UUID
