from pydantic import UUID4, BaseModel, EmailStr
from typing import List


class InvitationRequest(BaseModel):
    emails: List[EmailStr]
    project_ids: List[UUID4]

class InvitationResponse(BaseModel):
    id: UUID4
    email: EmailStr
    project_id: UUID4

class InvitationsResponse(BaseModel):
    invitations: List[InvitationResponse]