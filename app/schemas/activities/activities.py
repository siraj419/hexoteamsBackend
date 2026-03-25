from enum import Enum
from pydantic import BaseModel, UUID4
from typing import Optional, List

class ActivityType(Enum):
    TASK = "task"
    PROJECT = "project"

class ActivityResponse(BaseModel):
    id: UUID4
    user_display_name: str
    user_avatar_url: Optional[str] = None
    description: str
    activity_time: str

class ActivityGetPaginatedResponse(BaseModel):
    activities: List[ActivityResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None