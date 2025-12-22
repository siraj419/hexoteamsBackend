from pydantic import BaseModel, UUID4, Field
from datetime import datetime
from enum import Enum
from typing import Optional, List

class OrganizationMemberRole(Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

class Organization(BaseModel):
    id: UUID4
    name: str
    avatar_color: str
    avatar_icon: str
    avatar_file_id: UUID4
    created_by: UUID4
    created_at: datetime
    updated_at: datetime

class OrganizationMember(BaseModel):
    id: UUID4
    org_id: UUID4
    user_id: UUID4
    role: OrganizationMemberRole
    created_at: datetime
    updated_at: datetime

class OrganizationResponse(BaseModel):
    id: UUID4
    name: str
    description: Optional[str] = Field(None, description="The description of the organization", max_length=255)
    avatar_color: str
    avatar_icon: str

class OrganizationGetResponse(OrganizationResponse):
    avatar_url: Optional[str] = Field(None, description="The URL of the organization's avatar")
    member_role: Optional[OrganizationMemberRole] = Field(None, description="The role of the user in the organization")

class OrganizationCreateResponse(OrganizationResponse):
    pass

class OrganizationGetPaginatedResponse(BaseModel):
    organizations: List[OrganizationGetResponse]
    total: int
    offset: int
    limit: int


class OrganizationCreateRequest(BaseModel):
    name: str = Field(..., description="The name of the organization", max_length=255)
    description: Optional[str] = Field(None, description="The description of the organization", max_length=255)
    
class OrganizationUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, description="The name of the organization", max_length=255)
    description: Optional[str] = Field(None, description="The description of the organization", max_length=255)
    avatar_icon: Optional[str] = Field(None, description="The icon of the organization", max_length=255)
    avatar_color: Optional[str] = Field(None, description="The color of the organization", max_length=255)

class OrganizationUpdateResponse(OrganizationResponse):
    pass

class OrganizationChangeAvatarResponse(BaseModel):
    avatar_url: str