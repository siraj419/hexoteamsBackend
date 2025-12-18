from pydantic import BaseModel, UUID4
from typing import List, Optional, Dict


class FileUploadedByUser(BaseModel):
    id: UUID4
    display_name: str

class FileUploadedByUserResponse(FileUploadedByUser):
    pass

class FileUploadedByUserGetResponse(FileUploadedByUserResponse):
    avatar_url: Optional[str] = None


class FileBaseResponse(BaseModel):
    id: UUID4
    name: str
    size: str
    uploaded_by: UUID4
    is_deleted: bool
    content_type: str

class FileBaseResponseWithUploaderId(FileBaseResponse):
    uploaded_by_id: UUID4

class FileResponseWithUser(FileBaseResponse):
    uploaded_by: FileUploadedByUserGetResponse

class FileGetResponseWithUser(FileBaseResponse):
    uploaded_by: FileUploadedByUserGetResponse
    file_url: str


class FileGetPaginatedResponseWithUploaders(BaseModel):
    files: List[FileBaseResponse]
    uploaders: Dict[UUID4, FileUploadedByUserGetResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None