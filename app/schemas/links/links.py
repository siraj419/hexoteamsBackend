from pydantic import BaseModel, UUID4, AnyUrl
from enum import Enum
from typing import Optional, List


class LinkEntityType(Enum):
    TASK = 'task'
    PROJECT = 'project'

class LinkRequest(BaseModel):
    title: Optional[str] = None
    link_url: AnyUrl

class LinkUpdateRequest(BaseModel):
    title: Optional[str] = None
    link_url: Optional[AnyUrl] = None

class LinkResponse(BaseModel):
    id: UUID4
    title: Optional[str] = None
    link_url: AnyUrl
    created_time: str

class LinkGetPaginatedResponse(BaseModel):
    links: List[LinkResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None