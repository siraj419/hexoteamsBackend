import secrets
import uuid
from uuid import UUID
from fastapi import HTTPException, status
from pydantic import UUID4, EmailStr
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from supabase_auth.errors import AuthApiError

from app.core import supabase
from app.core.config import Settings
from app.schemas.teams import (
    TeamInviteRequest,
    TeamInvitationsResponse,
    TeamMembersResponse,
    TeamUserRole,
    TeamInvitedByResponse,
    TeamInvitationProjectResponse,
    TeamInvitationAcceptRequest,
    TeamInvitationAcceptResponse,
)
from app.schemas.organizations import OrganizationMemberRole
from app.schemas.projects import ProjectMemberRole
from app.services.organization import OrganizationService
from app.services.project import ProjectService
from app.services.files import FilesService
from app.utils import calculate_time_ago, apply_pagination
from app.utils.inbox_helpers import trigger_organization_invitation_notification
<<<<<<< HEAD
from app.utils.redis_cache import redis_client
=======
from app.utils.redis_cache import cache_service, UserCache
>>>>>>> 9ee6588f48ee12153325515421f8961ae6d6bdec
from app.tasks.tasks import send_email_task
import json

settings = Settings()

class TeamService:
    CACHE_TTL_INVITATIONS = 180  # 3 minutes
    CACHE_TTL_MEMBERS = 180  # 3 minutes
    CACHE_TTL_ORG = 600  # 10 minutes
    
    def __init__(self):
        self.organization_service = OrganizationService()
        self.project_service = ProjectService()
        self.files_service = FilesService()
    
    def invite_user(
        self,
        org_id: UUID4,
        invited_by: UUID4,
        invite_request: TeamInviteRequest,
        inviter_role: str,
    ) -> None:
        """
        Invite users to an organization and optionally add them to projects.
        
        Permission rules:
        - Owners can invite users with any role (admin or member)
        - Admins can only invite users as members (not as admin)
        
        Case 1: User already belongs to organization
        - Add user to specified project(s)
        - Send informational email
        - No invitation record created
        
        Case 2: User does not belong to organization
        - Create invitation record with token
        - Send invitation email with accept link
        """
        if not invite_request.user_emails:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one email is required"
            )
        
        if inviter_role == OrganizationMemberRole.ADMIN.value and invite_request.add_as_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins can only invite users as members. Only owners can invite users as admins."
            )
        
        # Verify projects belong to organization
        self._verify_projects_belong_to_org(org_id, invite_request.project_ids)
        
        # Get organization info for emails
        org_info = self._get_organization_info(org_id)
        inviter_info = self._get_user_info(invited_by)
        
        # Process each email
        for email in invite_request.user_emails:
            try:
                # Check if user exists and is already a member
                existing_user = self._get_user_by_email(email)
                
                if existing_user:
                    # Check if user is already a member of the organization
                    is_member = self._is_organization_member(org_id, existing_user['id'])
                    
                    if is_member:
                        if not invite_request.project_ids:
                            return
                        # Case 1: User already belongs to organization
                        self._add_user_to_projects(
                            existing_user['id'],
                            invite_request.project_ids,
                            project_ids=invite_request.project_ids,
                            add_as_admin=invite_request.add_as_admin,
                        )
                        # Send informational email (async)
                        self._send_project_addition_email(
                            email,
                            org_info['name'],
                            invite_request.project_ids,
                            inviter_info['display_name']
                        )
                    else:
                        # Case 2: User exists but not a member - create invitation
                        self._create_invitation(
                            org_id,
                            email,
                            invited_by,
                            invite_request.project_ids,
                            invite_request.add_as_admin
                        )
                else:
                    # Case 2: User doesn't exist - create invitation
                    self._create_invitation(
                        org_id,
                        email,
                        invited_by,
                        invite_request.project_ids if invite_request.project_ids else [],
                        invite_request.add_as_admin
                    )
            except Exception as e:
                # Log error but continue with other emails
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to process invitation for {email}: {str(e)}", exc_info=True)
                continue
        
        # Invalidate invitation caches
        cache_service.invalidate_pattern(f"team:invitations:{org_id}:*")
    
    def accept_invitation(
        self,
        accept_request: TeamInvitationAcceptRequest,
        user_id: Optional[UUID4] = None,
    ) -> TeamInvitationAcceptResponse:
        """
        Accept an organization invitation using a token.
        Handles authenticated, unauthenticated, and new user cases.
        Ensures idempotency.
        """
        # Validate token and get invitation
        invitation = self._validate_invitation_token(accept_request.token)
        
        # Check if already accepted
        if invitation.get('accepted_at'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation already accepted"
            )
        
        # If user_id provided, user is authenticated
        if user_id:
            # Verify email matches (if user exists)
            user_email = self._get_user_email(user_id)
            if user_email and user_email.lower() not in [e.lower() for e in invitation['email']]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email does not match invitation"
                )
            
            # Add user to organization and projects
            org_id = UUID4(invitation['org_id'])
            project_ids = [UUID4(pid) for pid in invitation.get('added_project_ids', [])] if invitation.get('added_project_ids') else []
            inviter_id = UUID4(invitation['invited_by']) if invitation.get('invited_by') else None
            self._add_user_to_organization_and_projects(
                user_id,
                org_id,
                project_ids,
                invitation.get('as_admin', False),
                inviter_id=inviter_id
            )
            
            # Mark invitation as accepted
            self._mark_invitation_accepted(UUID4(invitation['id']))
            
            # Set the invited organization as active for the user
            from app.services.organization import OrganizationService
            org_service = OrganizationService()
            org_service.set_active_organization(org_id, user_id)
            
            # Invalidate invitation and member caches
            cache_service.invalidate_pattern(f"team:invitations:{org_id}:*")
            cache_service.invalidate_pattern(f"team:members:{org_id}:*")
            
            return TeamInvitationAcceptResponse(
                success=True,
                message="Invitation accepted successfully",
                organization_id=org_id,
                project_ids=project_ids,
            )
        else:
            # User not authenticated - return info for frontend to handle
            # Frontend should redirect to sign-in/register, then call this again
            return TeamInvitationAcceptResponse(
                success=False,
                message="Please sign in or register to accept the invitation",
                organization_id=UUID4(invitation['org_id']),
                project_ids=[UUID4(pid) for pid in invitation.get('added_project_ids', [])] if invitation.get('added_project_ids') else [],
            )
    
    def get_team_invitations(
        self,
        org_id: UUID4,
        search: Optional[str] = None,
        limit: Optional[int] = 20,
        offset: Optional[int] = 0,
    ) -> Dict[str, Any]:
        """
        Get all invitations for an organization with pagination.
        """
        # Skip caching if search is provided (too dynamic)
        if not search:
            cache_key = f"team:invitations:{org_id}:{limit}:{offset}"
            cached = cache_service.get(cache_key)
            if cached:
                return cached
        
        query = supabase.table('invitations').select(
            'id, email, token, invited_by, accepted_at, expires_at, created_at, added_project_ids, as_admin',
            count='exact'
        ).eq('org_id', str(org_id)).order('created_at', desc=True)
        
        # For search with array columns, we need to fetch, filter, then paginate
        # Fetch a reasonable batch to account for filtering (up to 10x limit or 500, whichever is smaller)
        if search:
            fetch_limit = min((limit or 20) * 10, 500)
            query = query.limit(fetch_limit)
        else:
            limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get invitations: {e}"
            )
        
        # Apply search filter before processing (for array email column)
        filtered_data = response.data if response.data else []
        if search:
            filtered_data = [
                inv for inv in filtered_data
                if any(search.lower() in email.lower() for email in inv.get('email', []))
            ]
            # Apply pagination after filtering
            if limit and offset is not None:
                filtered_data = filtered_data[offset:offset + limit]
        
        total = len(filtered_data) if search else (response.count if hasattr(response, 'count') and response.count else 0)
        
        if not filtered_data:
            return {'invitations': [], 'total': total, 'limit': limit, 'offset': offset}
        
        # Get unique user IDs for batch fetching
        inviter_ids = set()
        for inv in filtered_data:
            if inv.get('invited_by'):
                inviter_ids.add(inv['invited_by'])
        
        inviters_cache = {}
        if inviter_ids:
            inviters_cache = self._batch_get_user_info([UUID4(uid) for uid in inviter_ids])
        
        # Get project info for each invitation
        all_project_ids = set()
        for inv in filtered_data:
            if inv.get('added_project_ids'):
                all_project_ids.update(inv['added_project_ids'])
        
        projects_cache = {}
        if all_project_ids:
            projects_cache = self._batch_get_project_info([UUID4(pid) for pid in all_project_ids])
        
        invitations = []
        for inv in filtered_data:
            
            expires_at = datetime.fromisoformat(inv['expires_at'].replace('Z', '+00:00'))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if inv.get('accepted_at'):
                status_str = "accepted"
            elif inv.get('invalidated_at'):
                status_str = "invalidated"
            elif expires_at < datetime.now(timezone.utc):
                status_str = "expired"
            else:
                status_str = "pending"
            
            invited_projects = []
            if inv.get('added_project_ids'):
                for project_id in inv['added_project_ids']:
                    project_info = projects_cache.get(str(project_id))
                    if project_info:
                        invited_projects.append(TeamInvitationProjectResponse(
                            id=UUID4(project_id),
                            name=project_info['name'],
                            avatar_color=project_info.get('avatar_color'),
                            avatar_icon=project_info.get('avatar_icon'),
                            avatar_url=project_info.get('avatar_url'),
                        ))
            
            invited_by_info = None
            if inv.get('invited_by'):
                inviter = inviters_cache.get(str(inv['invited_by']))
                if inviter:
                    invited_by_info = TeamInvitedByResponse(
                        id=UUID4(inviter['id']),
                        display_name=inviter['display_name'],
                        email=inviter['email'],
                        avatar_url=inviter.get('avatar_url'),
                    )
            
            invitations.append(TeamInvitationsResponse(
                id=UUID4(inv['id']),
                status=status_str,
                emails=inv['email'],
                invited_projects=invited_projects if invited_projects else None,
                invitation_time=calculate_time_ago(inv['created_at'], 'utc'),
                invited_by=invited_by_info,
                expires_at=datetime.fromisoformat(inv['expires_at'].replace('Z', '+00:00')) if inv.get('expires_at') else None,
                as_admin=inv.get('as_admin', False),
            ))
        
        result = {'invitations': invitations, 'total': total, 'limit': limit, 'offset': offset}
        
        # Cache result if no search
        if not search:
            cache_service.set(f"team:invitations:{org_id}:{limit}:{offset}", result, ttl=self.CACHE_TTL_INVITATIONS)
        
        return result
    
    def get_team_members(
        self,
        org_id: UUID4,
        search: Optional[str] = None,
        role: Optional[TeamUserRole] = None,
        limit: Optional[int] = 20,
        offset: Optional[int] = 0,
    ) -> Dict[str, Any]:
        """
        Get all members of an organization with pagination and filters.
        """
        # Skip caching if search is provided
        if not search:
            cache_key = f"team:members:{org_id}:{role}:{limit}:{offset}"
            cached = cache_service.get(cache_key)
            if cached:
                return cached
        
        query = supabase.table('organization_members').select(
            'user_id, role, created_at',
            count='exact'
        ).eq('org_id', str(org_id)).order('created_at', desc=True)
        
        if role:
            query = query.eq('role', role.value)
        
        # For search, we need to fetch user info first, so fetch more results to account for filtering
        if search:
            # Fetch up to 10x the limit or 500, whichever is smaller, to account for filtering
            fetch_limit = min((limit or 20) * 10, 500)
            query = query.limit(fetch_limit)
        else:
            limit, offset, query = apply_pagination(query, limit, offset)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get team members: {e}"
            )
        
        if not response.data:
            return {'members': [], 'total': 0, 'limit': limit, 'offset': offset}
        
        # Batch fetch user info
        user_ids = [UUID4(member['user_id']) for member in response.data]
        users_cache = self._batch_get_user_info(user_ids)
        
        members = []
        seen_user_ids = set()
        
        for member in response.data:
            user_id_str = str(member['user_id'])
            
            # Skip duplicates
            if user_id_str in seen_user_ids:
                continue
            seen_user_ids.add(user_id_str)
            
            user_info = users_cache.get(user_id_str)
            
            if not user_info:
                continue
            
            # Apply search filter if provided (filter on display_name and email from profiles)
            if search:
                search_lower = search.lower()
                display_name = user_info.get('display_name', '').lower()
                email = user_info.get('email', '').lower()
                if search_lower not in display_name and search_lower not in email:
                    continue
            
            members.append(TeamMembersResponse(
                id=user_info['id'],
                display_name=user_info['display_name'],
                email=user_info['email'],
                avatar_url=user_info.get('avatar_url'),
                role=TeamUserRole(member['role']),
            ))
        
        # Apply pagination after filtering if search was provided
        if search:
            total = len(members)
            if limit and offset is not None:
                members = members[offset:offset + limit]
        else:
            total = response.count if hasattr(response, 'count') and response.count else len(members)
        
        result = {'members': members, 'total': total, 'limit': limit, 'offset': offset}
        
        # Cache result if no search
        if not search:
            cache_service.set(f"team:members:{org_id}:{role}:{limit}:{offset}", result, ttl=self.CACHE_TTL_MEMBERS)
        
        return result
    
    def remove_user(
        self,
        org_id: UUID4,
        user_to_remove_id: UUID4,
    ) -> None:
        """
        Remove a user from the organization and all its projects.
        """
        
        try:
            # Get all project IDs belonging to this organization
            projects_response = supabase.table('projects').select('id').eq('org_id', str(org_id)).execute()
            if projects_response.data:
                project_ids = [p['id'] for p in projects_response.data]
                # Remove user from all organization projects
                supabase.table('project_members').delete().eq('user_id', str(user_to_remove_id)).in_('project_id', project_ids).execute()
            
            # Remove from organization
            supabase.table('organization_members').delete().eq('org_id', str(org_id)).eq('user_id', str(user_to_remove_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to remove user: {e}"
            )
        
        # Invalidate member caches
        cache_service.invalidate_pattern(f"team:members:{org_id}:*")
    
    def toggle_user_admin(
        self,
        org_id: UUID4,
        target_user_id: UUID4,
    ) -> Dict[str, Any]:
        """
        Toggle admin role for a user. Admins become members, members become admins.
        """
        
        # Get target user's current role
        target_member = self._get_organization_member(org_id, target_user_id)
        if target_member['role'] == 'owner':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change owner's role"
            )
        
        new_role = OrganizationMemberRole.MEMBER if target_member['role'] == 'admin' else OrganizationMemberRole.ADMIN
        
        try:
            supabase.table('organization_members').update({
                'role': new_role.value,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('org_id', str(org_id)).eq('user_id', str(target_user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update user role: {e}"
            )
        
        # Invalidate member caches
        cache_service.invalidate_pattern(f"team:members:{org_id}:*")
        
        return {'role': new_role.value}
    
    # Private helper methods
    
    def _verify_projects_belong_to_org(self, org_id: UUID4, project_ids: List[UUID4]) -> None:
        """Verify all projects belong to the organization."""
        if not project_ids:
            return
        
        try:
            response = supabase.table('projects').select('id, org_id').in_('id', [str(pid) for pid in project_ids]).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify projects: {e}"
            )
        
        if len(response.data) != len(project_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more projects not found"
            )
        
        for project in response.data:
            if str(project['org_id']) != str(org_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Project {project['id']} does not belong to this organization"
                )
    
    def _get_organization_info(self, org_id: UUID4) -> Dict[str, Any]:
        """Get organization information."""
        cache_key = f"organization:{org_id}"
        cached = cache_service.get(cache_key)
        if cached:
            return cached
        
        try:
            response = supabase.table('organizations').select('id, name').eq('id', str(org_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization: {e}"
            )
        
        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )
        
        org_data = response.data[0]
        cache_service.set(cache_key, org_data, ttl=self.CACHE_TTL_ORG)
        
        return org_data
    
    def _get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email from auth.users table."""
        try:
            # Check if user exists in profiles (which links to auth.users)
            response = supabase.table('profiles').select('user_id, email').eq('email', email.lower()).execute()
            if response.data and len(response.data) > 0:
                return {'id': response.data[0]['user_id'], 'email': response.data[0]['email']}
        except Exception:
            pass
        return None
    
    def _is_organization_member(self, org_id: UUID4, user_id: UUID4) -> bool:
        """Check if user is already a member of the organization."""
        try:
            response = supabase.table('organization_members').select('id').eq('org_id', str(org_id)).eq('user_id', str(user_id)).execute()
            return len(response.data) > 0
        except Exception:
            return False
    
    def _add_user_to_projects(
        self,
        user_id: UUID4,
        add_as_admin: bool,
        project_ids: List[UUID4] = [],
        inviter_id: Optional[UUID4] = None,
    ) -> None:
        """Add user to multiple projects."""
        role = ProjectMemberRole.ADMIN if add_as_admin else ProjectMemberRole.MEMBER
        
        for project_id in project_ids:
            try:
                # Check if already a member (idempotency)
                existing = supabase.table('project_members').select('id').eq('project_id', str(project_id)).eq('user_id', str(user_id)).execute()
                if existing.data:
                    continue  # Already a member, skip
                
                self.project_service._add_project_member(
                    project_id, 
                    user_id, 
                    role, 
                    added_by_id=inviter_id,
                    skip_notification=False
                )
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to add user {user_id} to project {project_id}: {str(e)}", exc_info=True)
                continue
    
    def _create_invitation(
        self,
        org_id: UUID4,
        email: str,
        invited_by: UUID4,
        project_ids: List[UUID4],
        add_as_admin: bool,
    ) -> None:
        """Create an invitation record and send invitation email."""
        # Invalidate existing pending invitations for same email + org
        self._invalidate_existing_invitations(org_id, email)
        
        # Generate secure token
        token = secrets.token_urlsafe(32)
        
        # Set expiration from config
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.INVITATION_TOKEN_EXPIRATION_HOURS)
        
        try:
            response = supabase.table('invitations').insert({
                'id': str(uuid.uuid4()),
                'org_id': str(org_id),
                'email': [email.lower()],
                'token': token,
                'as_admin': add_as_admin,
                'invited_by': str(invited_by),
                'expires_at': expires_at.isoformat(),
                'created_at': datetime.now(timezone.utc).isoformat(),
                'added_project_ids': [str(pid) for pid in project_ids] if project_ids else None,
            }).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create invitation: {e}"
            )
        
        # Store add_as_admin in a separate metadata approach or apply at acceptance
        # For now, we'll apply it when accepting based on a default (member role)
        # Send invitation email (async via Celery)
        org_info = self._get_organization_info(org_id)
        inviter_info = self._get_user_info(invited_by)
        project_names = self._get_project_names(project_ids)
        
        self._send_invitation_email(
            email,
            token,
            org_info['name'],
            project_names,
            inviter_info['display_name']
        )
        
        existing_user = self._get_user_by_email(email)
        if existing_user:
            try:
                trigger_organization_invitation_notification(
                    user_id=UUID4(existing_user['id']),
                    org_id=org_id,
                    org_name=org_info['name'],
                    inviter_id=invited_by,
                    inviter_name=inviter_info['display_name'],
                )
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to send organization invitation notification: {e}")
    
    def _invalidate_existing_invitations(self, org_id: UUID4, email: str) -> None:
        """Mark existing pending invitations as invalidated for the same email + org."""
        try:
            supabase.table('invitations').update({
                'invalidated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('org_id', str(org_id)).contains('email', [email.lower()]).is_('accepted_at', 'null').execute()
        except Exception:
            pass
    
    def _validate_invitation_token(self, token: str) -> Dict[str, Any]:
        """Validate invitation token and return invitation data."""
        try:
            response = supabase.table('invitations').select('*').eq('token', token).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to validate invitation: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invalid invitation token"
            )
        
        invitation = response.data[0]
        
        # Check if invitation has been invalidated
        if invitation.get('invalidated_at'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation token has been invalidated"
            )
        
        # Check expiration
        expires_at = datetime.fromisoformat(invitation['expires_at'].replace('Z', '+00:00'))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation token has expired"
            )
        
        return invitation
    
    def _add_user_to_organization_and_projects(
        self,
        user_id: UUID4,
        org_id: UUID4,
        project_ids: List[UUID4],
        add_as_admin: bool,
        inviter_id: Optional[UUID4] = None,
    ) -> None:
        """Add user to organization and projects atomically."""
        # Check if already a member (idempotency)
        if self._is_organization_member(org_id, user_id):
            # User already in org, just add to projects
            self._add_user_to_projects(user_id, add_as_admin, project_ids, inviter_id=inviter_id)
            return
        
        # Add to organization
        org_role = OrganizationMemberRole.ADMIN if add_as_admin else OrganizationMemberRole.MEMBER
        self.organization_service._add_organization_member(org_id, user_id, org_role)
        
        # Add to projects
        self._add_user_to_projects(user_id, add_as_admin, project_ids, inviter_id=inviter_id)
    
    def _mark_invitation_accepted(self, invitation_id: UUID4) -> None:
        """Mark invitation as accepted."""
        try:
            supabase.table('invitations').update({
                'accepted_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', str(invitation_id)).execute()
        except AuthApiError as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to mark invitation as accepted: {str(e)}", exc_info=True)
    
    def _get_user_email(self, user_id: UUID4) -> Optional[str]:
        """Get user email from profile."""
        try:
            response = supabase.table('profiles').select('email').eq('user_id', str(user_id)).execute()
            if response.data and len(response.data) > 0:
                return response.data[0].get('email')
        except Exception:
            pass
        return None
    
    def _get_user_info(self, user_id: UUID4) -> Dict[str, Any]:
        """Get user info from profile - Uses UserCache."""
        cached_user = UserCache.get_user(str(user_id))
        if cached_user:
            avatar_url = None
            if cached_user.get('avatar_file_id'):
                try:
                    avatar_url = self.files_service.get_file_url(UUID4(cached_user['avatar_file_id']))
                except Exception:
                    pass
            
            return {
                'id': cached_user['id'],
                'display_name': cached_user['display_name'],
                'email': cached_user.get('email'),
                'avatar_url': avatar_url,
            }
        
        # Fetch from database if not cached
        try:
            response = supabase.table('profiles').select('user_id, display_name, email, avatar_file_id').eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user info: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        profile = response.data[0]
        avatar_url = None
        if profile.get('avatar_file_id'):
            try:
                avatar_url = self.files_service.get_file_url(UUID4(profile['avatar_file_id']))
            except Exception:
                pass
        
        user_data = {
            'id': profile['user_id'],
            'display_name': profile['display_name'],
            'email': profile['email'],
            'avatar_url': avatar_url,
        }
        
        # Cache the user data
        user_data_for_cache = {
            'id': profile['user_id'],
            'display_name': profile['display_name'],
            'avatar_file_id': profile.get('avatar_file_id'),
        }
        UserCache.set_user(str(user_id), user_data_for_cache)
        
        return user_data
    
    def _batch_get_user_info(self, user_ids: List[UUID4]) -> Dict[str, Dict[str, Any]]:
<<<<<<< HEAD
        """Batch fetch user info with caching."""
        if not user_ids:
            return {}
        
        CACHE_TTL = 300  # 5 minutes cache
=======
        """Batch fetch user info - Uses UserCache for caching."""
        if not user_ids:
            return {}
        
>>>>>>> 9ee6588f48ee12153325515421f8961ae6d6bdec
        users_dict = {}
        uncached_user_ids = []
        
        # Check cache for each user
<<<<<<< HEAD
        if redis_client:
            for user_id in user_ids:
                user_id_str = str(user_id)
                cache_key = f"user_info:{user_id_str}"
                try:
                    cached = redis_client.get(cache_key)
                    if cached:
                        try:
                            cached_user = json.loads(cached)
                            # Validate cached data has required keys
                            if isinstance(cached_user, dict) and 'id' in cached_user and 'display_name' in cached_user and 'email' in cached_user:
                                users_dict[user_id_str] = cached_user
                            else:
                                # Invalid cache structure, fetch from DB
                                uncached_user_ids.append(user_id)
                        except (json.JSONDecodeError, KeyError, TypeError) as e:
                            # Invalid cache data, fetch from DB
                            uncached_user_ids.append(user_id)
                    else:
                        uncached_user_ids.append(user_id)
                except Exception as e:
                    # Cache error, fetch from DB
                    uncached_user_ids.append(user_id)
        else:
            uncached_user_ids = list(user_ids)
        
        # Fetch uncached users from database
=======
        for user_id in user_ids:
            user_id_str = str(user_id)
            cached_user = UserCache.get_user(user_id_str)
            if cached_user:
                avatar_url = None
                if cached_user.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(UUID4(cached_user['avatar_file_id']))
                    except Exception:
                        pass
                
                users_dict[user_id_str] = {
                    'id': cached_user['id'],
                    'display_name': cached_user['display_name'],
                    'email': cached_user.get('email'),
                    'avatar_url': avatar_url,
                }
            else:
                uncached_user_ids.append(user_id)
        
        # Batch fetch uncached users
>>>>>>> 9ee6588f48ee12153325515421f8961ae6d6bdec
        if uncached_user_ids:
            try:
                user_id_strings = [str(uid) if isinstance(uid, UUID) else uid for uid in uncached_user_ids]
                response = supabase.table('profiles').select('user_id, display_name, email, avatar_file_id').in_('user_id', user_id_strings).execute()
            except AuthApiError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to batch get user info: {e}"
                )
            
            for profile in response.data:
                avatar_url = None
                if profile.get('avatar_file_id'):
                    try:
<<<<<<< HEAD
                        avatar_url = self.files_service.get_file_url(profile['avatar_file_id'])
=======
                        avatar_url = self.files_service.get_file_url(UUID4(profile['avatar_file_id']))
>>>>>>> 9ee6588f48ee12153325515421f8961ae6d6bdec
                    except Exception:
                        pass
                
                user_id_str = str(profile['user_id'])
<<<<<<< HEAD
                user_data = {
=======
                users_dict[user_id_str] = {
>>>>>>> 9ee6588f48ee12153325515421f8961ae6d6bdec
                    'id': profile['user_id'],
                    'display_name': profile['display_name'],
                    'email': profile['email'],
                    'avatar_url': avatar_url,
                }
                
<<<<<<< HEAD
                users_dict[user_id_str] = user_data
                
                # Cache the user info
                if redis_client:
                    try:
                        cache_key = f"user_info:{user_id_str}"
                        redis_client.setex(cache_key, CACHE_TTL, json.dumps(user_data))
                    except Exception:
                        pass  # Continue even if caching fails
=======
                # Cache the user data
                user_data_for_cache = {
                    'id': profile['user_id'],
                    'display_name': profile['display_name'],
                    'avatar_file_id': profile.get('avatar_file_id'),
                }
                UserCache.set_user(user_id_str, user_data_for_cache)
>>>>>>> 9ee6588f48ee12153325515421f8961ae6d6bdec
        
        return users_dict
    
    def _batch_get_project_info(self, project_ids: List[UUID4]) -> Dict[str, Dict[str, Any]]:
        """Batch fetch project info - Uses caching similar to TaskService."""
        if not project_ids:
            return {}
        
        from app.utils.redis_cache import cache_service
        
        CACHE_TTL = 300  # 5 minutes
        projects_dict = {}
        uncached_project_ids = []
        
        # Check cache for each project
        for project_id in project_ids:
            project_id_str = str(project_id)
            cache_key = f"project_info:{project_id_str}"
            cached = cache_service.get(cache_key)
            if cached:
                projects_dict[project_id_str] = cached
            else:
                uncached_project_ids.append(project_id)
        
        # Batch fetch uncached projects
        if uncached_project_ids:
            try:
                project_id_strings = [str(pid) if isinstance(pid, uuid.UUID) else pid for pid in uncached_project_ids]
                response = supabase.table('projects').select('id, name, avatar_color, avatar_icon, avatar_file_id').in_('id', project_id_strings).execute()
            except AuthApiError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to batch get project info: {e}"
                )
            
            # Cache mapping for batch set
            cache_mapping = {}
            
            for project in response.data:
                avatar_url = None
                if project.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(UUID4(project['avatar_file_id']))
                    except Exception:
                        pass
                
                project_id_str = str(project['id'])
                project_data = {
                    'id': project['id'],
                    'name': project['name'],
                    'avatar_color': project.get('avatar_color'),
                    'avatar_icon': project.get('avatar_icon'),
                    'avatar_url': avatar_url,
                }
                
                projects_dict[project_id_str] = project_data
                cache_mapping[f"project_info:{project_id_str}"] = project_data
            
            # Batch cache all projects
            if cache_mapping:
                cache_service.set_many(cache_mapping, ttl=CACHE_TTL)
        
        return projects_dict
    
    def _get_project_names(self, project_ids: List[UUID4]) -> List[str]:
        """Get project names for email."""
        projects = self._batch_get_project_info(project_ids)
        if not projects:
            return []
        
        return [projects[str(pid)]['name'] for pid in project_ids if str(pid) in projects]
    
    def _get_organization_member(self, org_id: UUID4, user_id: UUID4) -> Dict[str, Any]:
        """Get organization member info."""
        try:
            response = supabase.table('organization_members').select('*').eq('org_id', str(org_id)).eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization member: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User is not a member of this organization"
            )
        
        return response.data[0]
    
    def _send_invitation_email(
        self,
        email: str,
        token: str,
        org_name: str,
        project_names: List[str],
        inviter_name: str,
    ) -> None:
        """Send invitation email via Celery."""
        
        frontend_url = settings.FRONTEND_URL
        accept_url = f"{frontend_url}/accept-invitation?token={token}"
        
        subject = f"You've been invited to join {org_name}"
        
        if project_names:
            has_projects_text = f" and collaborate on the following project(s)"
            project_line = f'<p><strong>Project(s):</strong> {", ".join(project_names)}</p>'
        else:
            has_projects_text = ""
            project_line = ""
        
        template_vars = {
            'org_name': org_name,
            'project_names': ', '.join(project_names) if project_names else '',
            'inviter_name': inviter_name,
            'accept_url': accept_url,
            'has_projects': has_projects_text,
            'project_line': project_line,
        }
        
        # Send via Celery task
        send_email_task.delay(
            to_email=email,
            subject=subject,
            email_template='invitation.html',  # You'll need to create this template
            body='',
            text_content=f"You've been invited to join {org_name}. Click here to accept: {accept_url}",
            token=token,
            template_vars=template_vars
        )
    
    def _send_project_addition_email(
        self,
        email: str,
        org_name: str,
        project_ids: List[UUID4],
        inviter_name: str,
    ) -> None:
        """Send informational email when user is added to projects."""
        
        project_names = self._get_project_names(project_ids)
        frontend_url = settings.FRONTEND_URL
        
        subject = f"You've been added to projects in {org_name}"
        template_vars = {
            'org_name': org_name,
            'project_names': ', '.join(project_names),
            'inviter_name': inviter_name,
            'frontend_url': frontend_url,
        }
        
        # Send via Celery task
        send_email_task.delay(
            to_email=email,
            subject=subject,
            email_template='project_addition.html',
            body='',
            text_content=f"You've been added to {', '.join(project_names)} in {org_name}. Visit {frontend_url} to view your projects.",
            token='',
            template_vars=template_vars
        )
