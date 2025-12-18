from fastapi import HTTPException, status
from pydantic import UUID4
from typing import Optional, List
from pydantic import Any

from app.core import supabase

class InvitationService:
    def __init__(self):
        pass
    
    def create_invitation(self, organization_id: UUID4, user_id: UUID4) -> InvitationCreateResponse:
        pass    