from __future__ import annotations

from pydantic import BaseModel, UUID4, Field, AnyUrl
from typing import Optional, List
from datetime import date, datetime

from enum import Enum
from app.schemas.activities import ActivityResponse

class ProjectTasksView(Enum):
    LIST = "list"
    CALENDAR = "calendar"
    BOARD = "board"
    
class ProjectMemberRole(Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

class ProjectOrderBy(Enum):
    ALPHABETICAL_ASC = "alphabetical_asc"
    ALPHABETICAL_DESC = "alphabetical_desc"
    DATE_CREATED_DESC = "created_desc"
    DATE_CREATED_ASC = "created_asc"



class ProjectCreateRequest(BaseModel):
    name: str
    avatar_color: Optional[str] = None
    avatar_icon: Optional[str] = None
    avatar_file_id: Optional[UUID4] = None
    start_date: date
    end_date: Optional[date] = None
    view: Optional[ProjectTasksView] = ProjectTasksView.LIST

class ProjectAddMemberRequest(BaseModel):
    user_id: UUID4
    role: ProjectMemberRole = ProjectMemberRole.MEMBER

    
class ProjectResponse(BaseModel):
    id: UUID4
    org_id: UUID4
    name: str
    avatar_color: Optional[str] = None
    avatar_icon: Optional[str] = None
    avatar_url: Optional[str] = None
    start_date: date
    end_date: Optional[date] = None
    view: Optional[ProjectTasksView] = ProjectTasksView.LIST
    progress_percentage: Optional[int] = 0
    members: List['ProjectMemberSummary'] = []

class AllProjectsResponse(BaseModel):
    member_projects: List[ProjectResponse]
    favourite_projects: List[ProjectResponse]
    non_member_projects_count: int
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None

class NonMemberProjectsResponse(BaseModel):
    projects: List[ProjectResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None

class ArchivedProjectsResponse(BaseModel):
    projects: List[ProjectResponse]
    total: int
    offset: Optional[int] = None
    limit: Optional[int] = None

class ProjectsResponse(BaseModel):
    member_projects: List[ProjectResponse]
    favourite_projects: List[ProjectResponse]
    non_member_projects: List[ProjectResponse]
    non_member_projects_count: int
    total: int
    offset: int
    limit: int


class ProjectGetPaginatedResponse(BaseModel):
    projects: List[ProjectResponse]
    total: int
    offset: int
    limit: int

class ProjectGetResponse(ProjectResponse):
    pass

class ProjectCreateResponse(ProjectResponse):
    pass

class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None
    avatar_color: Optional[str] = None
    avatar_icon: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    view: Optional[ProjectTasksView] = None

class ProjectUpdateResponse(ProjectResponse):
    pass

class ProjectDeleteResponse(BaseModel):
    message: str


class ProjectChangeAvatarResponse(BaseModel):
    avatar_url: str

class ProjectMember(BaseModel):
    id: UUID4
    project_id: UUID4
    user_id: UUID4
    role: ProjectMemberRole
    created_at: datetime
    updated_at: datetime


class ProjectMemberSummary(BaseModel):
    id: UUID4
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


class ProjectAttachmentSummary(BaseModel):
    id: UUID4
    file_name: str
    file_size: str
    content_type: str
    created_at: datetime


class ProjectLinkSummary(BaseModel):
    id: UUID4
    title: Optional[str] = None
    link_url: str
    created_at: datetime


class TaskSummary(BaseModel):
    completed: int = 0
    incomplete: int = 0
    overdue: int = 0


class UserWorkload(BaseModel):
    user_id: UUID4
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    task_count: int = 0
    percentage: float = 0.0


class TeamWorkload(BaseModel):
    assigned_percentage: float = 0.0
    unassigned_percentage: float = 0.0
    user_workloads: List[UserWorkload] = []


class ProjectSummaryResponse(BaseModel):
    project: Optional[ProjectResponse] = None
    members: List[ProjectMemberSummary] = []
    latest_attachments: List[ProjectAttachmentSummary] = []
    latest_links: List[ProjectLinkSummary] = []
    task_summary: TaskSummary
    team_workload: TeamWorkload
    recent_activities: List[ActivityResponse] = []
    