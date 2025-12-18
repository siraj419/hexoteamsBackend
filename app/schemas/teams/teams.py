from pydantic import BaseModel, UUID4, EmailStr
from typing import List, Optional
from datetime import datetime, timedelta
from enum import Enum


class TeamUserRole(Enum):
    ADMIN = "admin"
    MEMBER = "member"
    OWNER = "owner"
    
class TeamInviteRequest(BaseModel):
    user_emails: List[EmailStr]
    add_as_admin: bool = False
    project_ids: Optional[List[UUID4]] = None


class TeamInvitationProjectResponse(BaseModel):
    id: UUID4
    name: str
    avatar_color: Optional[str] = None
    avatar_icon: Optional[str] = None
    avatar_url: Optional[str] = None

class TeamUserResponse(BaseModel):
    id: UUID4
    display_name: str
    email: EmailStr
    avatar_url: Optional[str] = None

class TeamInvitedByResponse(TeamUserResponse):
    pass

class TeamInvitationsResponse(BaseModel):
    id: UUID4
    status: str
    emails: List[EmailStr]
    invited_projects: Optional[List[TeamInvitationProjectResponse]] = None
    invitation_time: str
    invited_by: Optional[TeamInvitedByResponse] = None
    expires_at: Optional[datetime] = None
    as_admin: bool = False

class TeamMembersResponse(TeamUserResponse):
    role: TeamUserRole

class TeamInvitationAcceptRequest(BaseModel):
    token: str

class TeamInvitationAcceptResponse(BaseModel):
    success: bool
    message: str
    organization_id: UUID4
    project_ids: List[UUID4]


class PaginatedTeamInvitationsResponse(BaseModel):
    invitations: List[TeamInvitationsResponse]
    total: int
    limit: Optional[int] = None
    offset: Optional[int] = None


class PaginatedTeamMembersResponse(BaseModel):
    members: List[TeamMembersResponse]
    total: int
    limit: Optional[int] = None
    offset: Optional[int] = None