from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from pydantic import UUID4
from typing import List, Optional, Any

from app.routers.deps import get_organization_admin_or_owner, get_current_user, get_organization_owner
from app.schemas.teams import (
    TeamInviteRequest, 
    TeamInvitationsResponse, 
    TeamMembersResponse, 
    TeamUserRole,
    TeamInvitationAcceptRequest,
    TeamInvitationAcceptResponse,
    PaginatedTeamInvitationsResponse,
    PaginatedTeamMembersResponse,
)
from app.services.team import TeamService

router = APIRouter()


@router.post('/invite', status_code=status.HTTP_204_NO_CONTENT)
def invite_user(
    team_request: TeamInviteRequest,
    active_organization: Any = Depends(get_organization_admin_or_owner),
):
    """
    Invite users to an organization and optionally add them to projects.
    
    Case 1: User already belongs to organization
    - Adds user to specified project(s)
    - Sends informational email
    - No invitation record created
    
    Case 2: User does not belong to organization
    - Creates invitation record with secure token
    - Sends invitation email with accept link
    """
    team_service = TeamService()
    team_service.invite_user(
        active_organization['id'], 
        active_organization['member_user_id'],
        team_request
    )
    return None

@router.post('/accept-invitation', response_model=TeamInvitationAcceptResponse, status_code=status.HTTP_200_OK)
def accept_invitation(
    accept_request: TeamInvitationAcceptRequest,
    request: Request,
):
    """
    Accept an organization invitation using a token.
    
    Validates token existence and expiration.
    If user is authenticated (via Authorization header), adds them to organization and projects.
    If user is not authenticated, returns info for frontend to handle sign-in/registration.
    Ensures idempotency and prevents duplicate memberships.
    """
    team_service = TeamService()
    
    # Try to get current user if authenticated, but don't require it
    user_id = None
    try:
        if request.headers.get("Authorization"):
            user = get_current_user(request)
            user_id = UUID4(user.id) if user and hasattr(user, 'id') else None
    except:
        pass  # User not authenticated, which is fine
    
    return team_service.accept_invitation(accept_request, user_id)

@router.delete('/{user_id}/remove', status_code=status.HTTP_204_NO_CONTENT)
def remove_user(
    user_id: UUID4,
    active_organization: Any = Depends(get_organization_owner),
):
    """
    Remove a user from the organization.
    Only admins and owners can remove members.
    Cannot remove organization owner.
    """
    
    if str(user_id) == str(active_organization['member_user_id']):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove yourself"
        )
    
    
    team_service = TeamService()
    team_service.remove_user(
        active_organization['id'], 
        user_id
    )
    return None

@router.get('/invitations', response_model=PaginatedTeamInvitationsResponse, status_code=status.HTTP_200_OK)
def get_team_invitations(
    active_organization: Any = Depends(get_organization_admin_or_owner),
    search: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
    Get paginated team invitations for the organization
    """
    team_service = TeamService()
    return team_service.get_team_invitations(
        active_organization['id'], 
        search=search,
        limit=limit, 
        offset=offset
    )

@router.get('/members', response_model=PaginatedTeamMembersResponse, status_code=status.HTTP_200_OK)
def get_team_members(
    active_organization: Any = Depends(get_organization_admin_or_owner),
    search: Optional[str] = Query(None),
    role: Optional[TeamUserRole] = Query(None),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
    Get paginated team members for the organization
    """
    team_service = TeamService()
    return team_service.get_team_members(
        active_organization['id'],
        search=search,
        role=role,
        limit=limit,
        offset=offset,
    )

@router.patch('/{user_id}/admin', status_code=status.HTTP_200_OK)
def toggle_user_admin(
    user_id: UUID4,
    active_organization: Any = Depends(get_organization_owner),
):
    """
    Toggle admin role for a user. Admins become members, members become admins.
    Only owners can toggle admin status.
    """
    team_service = TeamService()
    return team_service.toggle_user_admin(
        active_organization['id'], 
        user_id
    )