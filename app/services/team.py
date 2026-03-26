import secrets
import uuid
from uuid import UUID
from fastapi import HTTPException, status
from pydantic import UUID4

from app.utils.uuid_compat import as_uuid
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from sqlalchemy import delete, func, select, update

from app.core.config import Settings
from app.db.sync_session import SyncSessionLocal
from app.models import Invitation, Organization, OrganizationMember, Profile, Project
from app.models.project_member import ProjectMember as ProjectMemberRow
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
from app.utils import calculate_time_ago
from app.utils.sa_pagination import apply_sa_limit_offset
from app.utils.inbox_helpers import trigger_organization_invitation_notification

from app.utils.redis_cache import cache_service, UserCache
from app.tasks.tasks import send_email_task
import logging

logger = logging.getLogger(__name__)

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
        
        # Invalidate any previous invitations that match exactly:
        # - Same email list (all emails in this invite_request)
        # - Same project list (all projects in this invite_request)
        # - Same organization
        # This ensures old invitation tokens are invalidated only when the exact same invitation is re-sent
        # Do this once before processing emails to avoid duplicate invalidations
        self._invalidate_existing_invitations(
            org_id, 
            invite_request.user_emails, 
            invite_request.project_ids if invite_request.project_ids else []
        )
        
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
                            as_uuid(existing_user["id"]),
                            invite_request.add_as_admin,
                            invite_request.project_ids,
                            inviter_id=invited_by,
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
        
        # Status check is already done in _validate_invitation_token, but keep this for backward compatibility
        if invitation.get('accepted_at') or invitation.get('status') == 'accepted':
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
            
            # Mark invitation as accepted IMMEDIATELY to prevent concurrent processing
            # This must happen before adding user to org/projects to prevent race conditions
            invitation_id = as_uuid(invitation['id'])
            try:
                db = SyncSessionLocal()
                try:
                    res = db.execute(
                        update(Invitation)
                        .where(
                            Invitation.id == UUID(str(invitation_id)),
                            Invitation.status == "pending",
                        )
                        .values(
                            status="accepted",
                            accepted_at=datetime.now(timezone.utc),
                        )
                    )
                    db.commit()
                    if not res.rowcount:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invitation already accepted by another request",
                        )
                except HTTPException:
                    db.rollback()
                    raise
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(
                    "Failed to mark invitation as accepted: %s", e, exc_info=True
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to process invitation",
                )
            
            # Add user to organization and projects
            org_id = as_uuid(invitation['org_id'])
            project_ids = [as_uuid(pid) for pid in invitation.get('added_project_ids', [])] if invitation.get('added_project_ids') else []
            inviter_id = as_uuid(invitation['invited_by']) if invitation.get('invited_by') else None
            self._add_user_to_organization_and_projects(
                user_id,
                org_id,
                project_ids,
                invitation.get('as_admin', False),
                inviter_id=inviter_id
            )
            
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
                organization_id=as_uuid(invitation['org_id']),
                project_ids=[as_uuid(pid) for pid in invitation.get('added_project_ids', [])] if invitation.get('added_project_ids') else [],
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
        
        oid = UUID(str(org_id))

        def _inv_dict(inv: Invitation) -> Dict[str, Any]:
            return {
                "id": str(inv.id),
                "email": list(inv.email),
                "token": inv.token,
                "invited_by": str(inv.invited_by) if inv.invited_by else None,
                "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
                "expires_at": inv.expires_at.isoformat(),
                "created_at": inv.created_at.isoformat(),
                "added_project_ids": [str(x) for x in (inv.added_project_ids or [])],
                "as_admin": inv.as_admin,
                "status": inv.status,
            }

        db = SyncSessionLocal()
        try:
            base = (
                select(Invitation)
                .where(Invitation.org_id == oid)
                .order_by(Invitation.created_at.desc())
            )
            if search:
                fetch_limit = min((limit or 20) * 10, 500)
                raw_rows = list(db.scalars(base.limit(fetch_limit)).all())
            else:
                lim, off, page = apply_sa_limit_offset(base, limit, offset)
                raw_rows = list(db.scalars(page).all())
                total = (
                    db.scalar(
                        select(func.count())
                        .select_from(Invitation)
                        .where(Invitation.org_id == oid)
                    )
                    or 0
                )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get invitations: {e}",
            )
        finally:
            db.close()

        filtered_data = [_inv_dict(r) for r in raw_rows]

        if search:
            filtered_data = [
                inv
                for inv in filtered_data
                if any(
                    search.lower() in email.lower()
                    for email in inv.get("email", [])
                )
            ]
            total = len(filtered_data)
            if limit and offset is not None:
                filtered_data = filtered_data[offset : offset + limit]
        
        if not filtered_data:
            return {'invitations': [], 'total': total, 'limit': limit, 'offset': offset}
        
        # Get unique user IDs for batch fetching
        inviter_ids = set()
        for inv in filtered_data:
            if inv.get('invited_by'):
                inviter_ids.add(inv['invited_by'])
        
        inviters_cache = {}
        if inviter_ids:
            inviters_cache = self._batch_get_user_info([as_uuid(uid) for uid in inviter_ids])
        
        # Get project info for each invitation
        all_project_ids = set()
        for inv in filtered_data:
            if inv.get('added_project_ids'):
                all_project_ids.update(inv['added_project_ids'])
        
        projects_cache = {}
        if all_project_ids:
            projects_cache = self._batch_get_project_info([as_uuid(pid) for pid in all_project_ids])
        
        invitations = []
        for inv in filtered_data:
            # Use status from database if available, otherwise calculate based on accepted_at and expiration
            status_str = inv.get('status', 'pending')
            
            # If status is not set in DB, calculate it (for backward compatibility)
            if not status_str or status_str == 'pending':
                expires_at = datetime.fromisoformat(inv['expires_at'].replace('Z', '+00:00'))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                
                if inv.get('accepted_at'):
                    status_str = "accepted"
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
                            id=as_uuid(project_id),
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
                        id=as_uuid(inviter['id']),
                        display_name=inviter['display_name'],
                        email=inviter['email'],
                        avatar_url=inviter.get('avatar_url'),
                    )
            
            invitations.append(TeamInvitationsResponse(
                id=as_uuid(inv['id']),
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
    
    def _regenerate_avatar_urls_for_cached_members(self, cached_members: List[Dict[str, Any]]) -> List[TeamMembersResponse]:
        """Regenerate avatar URLs for cached members since presigned URLs expire."""
        members_list = []
        for member_dict in cached_members:
            try:
                if not member_dict.get('email'):
                    logger.warning(f"Cached member {member_dict.get('id')} has no email, skipping")
                    continue
                
                # Regenerate avatar URL - use _get_user_info to ensure we get fresh data
                user_id_str = str(member_dict.get('id', ''))
                if user_id_str:
                    try:
                        user_info = self._get_user_info(as_uuid(user_id_str))
                        member_dict['avatar_url'] = user_info.get('avatar_url')
                        logger.debug(f"Regenerated avatar URL for cached member {user_id_str}: {member_dict.get('avatar_url', 'None')[:50] if member_dict.get('avatar_url') else 'None'}...")
                    except Exception as e:
                        logger.warning(f"Failed to get user info for cached member {user_id_str}: {e}")
                        member_dict['avatar_url'] = None
                else:
                    member_dict['avatar_url'] = None
                
                # Normalize role to string
                if 'role' in member_dict:
                    role_value = member_dict['role']
                    if isinstance(role_value, TeamUserRole):
                        member_dict['role'] = role_value.value
                    elif not isinstance(role_value, str):
                        member_dict['role'] = 'member'
                
                members_list.append(TeamMembersResponse(**member_dict))
            except Exception as e:
                logger.warning(f"Failed to reconstruct cached member {member_dict.get('id')}: {e}")
                return []  # Return empty to force DB fetch
        
        return members_list
    
    def _build_team_member_from_data(self, member_data: Dict[str, Any], user_info: Dict[str, Any], search: Optional[str] = None) -> Optional[TeamMembersResponse]:
        """Build a TeamMembersResponse from member and user data, applying search filter if provided."""
        email = user_info.get('email')
        if not email:
            logger.warning(f"User {user_info.get('id')} has no email, skipping")
            return None
        
        # Apply search filter if provided
        if search:
            search_lower = search.lower()
            display_name = user_info.get('display_name', '').lower()
            email_lower = email.lower()
            if search_lower not in display_name and search_lower not in email_lower:
                return None
        
        # Normalize role
        role_value = member_data['role']
        role_enum = role_value if isinstance(role_value, TeamUserRole) else TeamUserRole(str(role_value))
        
        return TeamMembersResponse(
            id=user_info['id'],
            display_name=user_info.get('display_name', ''),
            email=email,
            avatar_url=user_info.get('avatar_url'),
            role=role_enum,
        )
    
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
        # Try to get from cache if no search
        if not search:
            cache_key = f"team:members:{org_id}:{role}:{limit}:{offset}"
            cached = cache_service.get(cache_key)
            if cached and cached.get('members'):
                members_list = self._regenerate_avatar_urls_for_cached_members(cached.get('members', []))
                if members_list and len(members_list) == len(cached.get('members', [])):
                    return {
                        'members': members_list,
                        'total': cached.get('total', 0),
                        'limit': cached.get('limit'),
                        'offset': cached.get('offset')
                    }
        
        oid = UUID(str(org_id))
        total = 0
        db = SyncSessionLocal()
        try:
            filt = OrganizationMember.org_id == oid
            if role:
                filt = filt & (OrganizationMember.role == role.value)
            base = (
                select(OrganizationMember)
                .where(filt)
                .order_by(OrganizationMember.created_at.desc())
            )
            if search:
                fetch_limit = min((limit or 20) * 10, 500)
                rows = list(db.scalars(base.limit(fetch_limit)).all())
            else:
                lim, off, page = apply_sa_limit_offset(base, limit, offset)
                rows = list(db.scalars(page).all())
                total = (
                    db.scalar(
                        select(func.count()).select_from(OrganizationMember).where(filt)
                    )
                    or 0
                )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get team members: {e}",
            )
        finally:
            db.close()

        response_data = [
            {
                "user_id": str(m.user_id),
                "role": m.role,
                "created_at": m.created_at.isoformat()
                if hasattr(m.created_at, "isoformat")
                else m.created_at,
            }
            for m in rows
        ]

        if not response_data:
            return {"members": [], "total": 0, "limit": limit, "offset": offset}

        user_ids = [as_uuid(member["user_id"]) for member in response_data]
        users_cache = self._batch_get_user_info(user_ids)
        
        # Build member list
        members = []
        seen_user_ids = set()
        
        for member_data in response_data:
            user_id_str = str(member_data["user_id"])
            
            # Skip duplicates
            if user_id_str in seen_user_ids:
                continue
            seen_user_ids.add(user_id_str)
            
            # Get user info (from cache or fetch if missing)
            user_info = users_cache.get(user_id_str)
            if not user_info:
                try:
                    user_info = self._get_user_info(as_uuid(user_id_str))
                    if not user_info:
                        logger.warning(f"User {user_id_str} not found in profiles, skipping")
                        continue
                    users_cache[user_id_str] = user_info
                except Exception as e:
                    logger.error(f"Failed to fetch user info for {user_id_str}: {e}")
                    continue
            
            # Build team member response
            team_member = self._build_team_member_from_data(member_data, user_info, search)
            if team_member:
                members.append(team_member)
        
        if search:
            total = len(members)
            if limit and offset is not None:
                members = members[offset : offset + limit]
        
        result = {'members': members, 'total': total, 'limit': limit, 'offset': offset}
        
        # Cache result if no search
        if not search:
            cache_data = {
                'members': [member.model_dump(mode='json') for member in members],
                'total': total,
                'limit': limit,
                'offset': offset
            }
            cache_service.set(cache_key, cache_data, ttl=self.CACHE_TTL_MEMBERS)
        
        return result
    
    def remove_user(
        self,
        org_id: UUID4,
        user_to_remove_id: UUID4,
    ) -> None:
        """
        Remove a user from the organization and all its projects.
        """
        
        oid, uid = UUID(str(org_id)), UUID(str(user_to_remove_id))
        db = SyncSessionLocal()
        try:
            pids = list(
                db.scalars(select(Project.id).where(Project.org_id == oid)).all()
            )
            if pids:
                db.execute(
                    delete(ProjectMemberRow).where(
                        ProjectMemberRow.user_id == uid,
                        ProjectMemberRow.project_id.in_(pids),
                    )
                )
            db.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.org_id == oid,
                    OrganizationMember.user_id == uid,
                )
            )
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to remove user: {e}",
            )
        finally:
            db.close()
        
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
        
        db = SyncSessionLocal()
        try:
            db.execute(
                update(OrganizationMember)
                .where(
                    OrganizationMember.org_id == UUID(str(org_id)),
                    OrganizationMember.user_id == UUID(str(target_user_id)),
                )
                .values(
                    role=new_role.value,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update user role: {e}",
            )
        finally:
            db.close()
        
        # Invalidate member caches
        cache_service.invalidate_pattern(f"team:members:{org_id}:*")
        
        return {'role': new_role.value}
    
    # Private helper methods
    
    def _verify_projects_belong_to_org(self, org_id: UUID4, project_ids: List[UUID4]) -> None:
        """Verify all projects belong to the organization."""
        if not project_ids:
            return
        
        puuids = [UUID(str(pid)) for pid in project_ids]
        oid = UUID(str(org_id))
        db = SyncSessionLocal()
        try:
            rows = db.execute(
                select(Project.id, Project.org_id).where(Project.id.in_(puuids))
            ).all()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify projects: {e}",
            )
        finally:
            db.close()

        if len(rows) != len(project_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more projects not found",
            )

        for pid, prow_org in rows:
            if prow_org != oid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Project {pid} does not belong to this organization",
                )
    
    def _get_organization_info(self, org_id: UUID4) -> Dict[str, Any]:
        """Get organization information."""
        cache_key = f"organization:{org_id}"
        cached = cache_service.get(cache_key)
        if cached:
            return cached
        
        db = SyncSessionLocal()
        try:
            row = db.execute(
                select(Organization.id, Organization.name).where(
                    Organization.id == UUID(str(org_id))
                )
            ).one_or_none()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization: {e}",
            )
        finally:
            db.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )

        org_data = {"id": str(row[0]), "name": row[1]}
        cache_service.set(cache_key, org_data, ttl=self.CACHE_TTL_ORG)
        
        return org_data
    
    def _get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email from auth.users table."""
        db = SyncSessionLocal()
        try:
            r = db.execute(
                select(Profile.user_id, Profile.email).where(
                    Profile.email == email.lower()
                )
            ).one_or_none()
            if r:
                return {"id": str(r[0]), "email": r[1]}
        except Exception:
            pass
        finally:
            db.close()
        return None
    
    def _is_organization_member(self, org_id: UUID4, user_id: UUID4) -> bool:
        """Check if user is already a member of the organization."""
        try:
            db = SyncSessionLocal()
            try:
                n = db.scalar(
                    select(func.count())
                    .select_from(OrganizationMember)
                    .where(
                        OrganizationMember.org_id == UUID(str(org_id)),
                        OrganizationMember.user_id == UUID(str(user_id)),
                    )
                )
                return bool(n and n > 0)
            finally:
                db.close()
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
                db = SyncSessionLocal()
                try:
                    n = db.scalar(
                        select(func.count())
                        .select_from(ProjectMemberRow)
                        .where(
                            ProjectMemberRow.project_id == UUID(str(project_id)),
                            ProjectMemberRow.user_id == UUID(str(user_id)),
                        )
                    )
                finally:
                    db.close()
                if n and n > 0:
                    continue

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
        # Note: Invalidation is handled in invite_user() before calling this method
        # to ensure we have the full email list and project list for exact matching
        
        # Generate secure token
        token = secrets.token_urlsafe(32)
        
        # Set expiration from config
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.INVITATION_TOKEN_EXPIRATION_HOURS)
        
        db = SyncSessionLocal()
        try:
            inv = Invitation(
                id=uuid.uuid4(),
                org_id=UUID(str(org_id)),
                email=[email.lower()],
                token=token,
                as_admin=add_as_admin,
                invited_by=UUID(str(invited_by)),
                expires_at=expires_at,
                created_at=datetime.now(timezone.utc),
                added_project_ids=[UUID(str(p)) for p in project_ids] if project_ids else None,
                status="pending",
            )
            db.add(inv)
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create invitation: {e}",
            )
        finally:
            db.close()
        
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
                    user_id=as_uuid(existing_user['id']),
                    org_id=org_id,
                    org_name=org_info['name'],
                    inviter_id=invited_by,
                    inviter_name=inviter_info['display_name'],
                )
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to send organization invitation notification: {e}")
    
    def _invalidate_existing_invitations(
        self, 
        org_id: UUID4, 
        email_list: List[str], 
        project_ids: List[UUID4]
    ) -> None:
        """
        Invalidate existing pending invitations that match exactly:
        - Same organization
        - Same email list (exact match, order doesn't matter)
        - Same project list (exact match, order doesn't matter)
        """
        db = SyncSessionLocal()
        try:
            rows = list(
                db.scalars(
                    select(Invitation).where(
                        Invitation.org_id == UUID(str(org_id)),
                        Invitation.status == "pending",
                    )
                ).all()
            )
        except Exception as e:
            logger.warning(
                "Failed to load invitations for invalidation org %s: %s", org_id, e
            )
            return
        finally:
            db.close()

        if not rows:
            return

        new_email_set = set(e.lower() for e in email_list if e)
        new_project_set = set(str(pid) for pid in project_ids) if project_ids else set()

        invitation_ids_to_invalidate = []

        try:
            for inv in rows:
                inv_emails = list(inv.email) if inv.email else []
                existing_email_set = set(e.lower() for e in inv_emails if e)
                inv_projects = inv.added_project_ids or []
                existing_project_set = set(str(pid) for pid in inv_projects if pid)
                emails_match = new_email_set == existing_email_set
                projects_match = new_project_set == existing_project_set
                if emails_match and projects_match:
                    invitation_ids_to_invalidate.append(inv.id)

            if invitation_ids_to_invalidate:
                db2 = SyncSessionLocal()
                try:
                    for inv_id in invitation_ids_to_invalidate:
                        try:
                            db2.execute(
                                update(Invitation)
                                .where(Invitation.id == inv_id)
                                .values(status="invalidated")
                            )
                        except Exception as ex:
                            logger.warning(
                                "Failed to invalidate invitation %s: %s", inv_id, ex
                            )
                    db2.commit()
                except Exception:
                    db2.rollback()
                    raise
                finally:
                    db2.close()
        except Exception as e:
            logger.warning(
                "Failed to invalidate existing invitations for emails %s and projects %s in org %s: %s",
                email_list,
                project_ids,
                org_id,
                e,
            )
    
    def _validate_invitation_token(self, token: str) -> Dict[str, Any]:
        """Validate invitation token and return invitation data."""
        db = SyncSessionLocal()
        try:
            row = db.scalar(select(Invitation).where(Invitation.token == token))
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to validate invitation: {e}",
            )
        finally:
            db.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invalid invitation token",
            )

        invitation = {
            "id": str(row.id),
            "org_id": str(row.org_id),
            "email": list(row.email),
            "token": row.token,
            "as_admin": row.as_admin,
            "invited_by": str(row.invited_by) if row.invited_by else None,
            "expires_at": row.expires_at.isoformat(),
            "created_at": row.created_at.isoformat(),
            "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
            "added_project_ids": [str(x) for x in (row.added_project_ids or [])],
            "status": row.status,
        }

        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        current_time = datetime.now(timezone.utc)
        is_expired = expires_at < current_time

        invitation_status = invitation.get("status", "pending")

        if is_expired and invitation_status == "pending":
            try:
                db2 = SyncSessionLocal()
                try:
                    db2.execute(
                        update(Invitation)
                        .where(Invitation.id == row.id)
                        .values(status="expired")
                    )
                    db2.commit()
                except Exception:
                    db2.rollback()
                    raise
                finally:
                    db2.close()
                invitation_status = "expired"
                invitation["status"] = "expired"
            except Exception as e:
                logger.warning(
                    "Failed to update invitation status to expired: %s", e
                )
        
        # Check status - reject if not pending
        if invitation_status == 'invalidated':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation has been invalidated"
            )
        if invitation_status == 'accepted':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation has already been accepted"
            )
        if invitation_status == 'expired':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invitation token has expired"
            )
        if invitation_status != 'pending':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invitation status is invalid: {invitation_status}"
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
            db = SyncSessionLocal()
            try:
                db.execute(
                    update(Invitation)
                    .where(Invitation.id == UUID(str(invitation_id)))
                    .values(
                        accepted_at=datetime.now(timezone.utc),
                        status="accepted",
                    )
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        except Exception as e:
            logger.error(
                "Failed to mark invitation as accepted: %s", e, exc_info=True
            )
    
    def _get_user_email(self, user_id: UUID4) -> Optional[str]:
        """Get user email from profile."""
        db = SyncSessionLocal()
        try:
            em = db.scalar(
                select(Profile.email).where(Profile.user_id == UUID(str(user_id)))
            )
            return em
        except Exception:
            return None
        finally:
            db.close()
    
    def _get_user_info(self, user_id: UUID4) -> Dict[str, Any]:
        """Get user info from profile - Uses UserCache."""
        user_id_str = str(user_id)
        cached_user = UserCache.get_user(user_id_str)
        if cached_user:
            avatar_url = None
            avatar_file_id = cached_user.get('avatar_file_id')
            if avatar_file_id:
                try:
                    avatar_url = self.files_service.get_file_url(as_uuid(avatar_file_id))
                    logger.debug(f"Generated avatar URL for user {user_id_str} from cache: {avatar_url[:50] if avatar_url else 'None'}...")
                except Exception as e:
                    logger.warning(f"Failed to get avatar URL for user {user_id_str} from cache (file_id: {avatar_file_id}): {e}")
                    avatar_url = None
            else:
                logger.debug(f"Cached user {user_id_str} has no avatar_file_id")
            
            # Handle both 'id' and 'user_id' keys for cache compatibility
            user_id_value = cached_user.get('id') or cached_user.get('user_id') or str(user_id)
            
            return {
                'id': user_id_value,
                'display_name': cached_user.get('display_name', ''),
                'email': cached_user.get('email'),
                'avatar_url': avatar_url,
            }
        
        db = SyncSessionLocal()
        try:
            row = db.execute(
                select(
                    Profile.user_id,
                    Profile.display_name,
                    Profile.email,
                    Profile.avatar_file_id,
                ).where(Profile.user_id == UUID(str(user_id)))
            ).one_or_none()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user info: {e}",
            )
        finally:
            db.close()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        profile = {
            "user_id": row[0],
            "display_name": row[1],
            "email": row[2],
            "avatar_file_id": row[3],
        }
        avatar_url = None
        avatar_file_id = profile.get('avatar_file_id')
        if avatar_file_id:
            try:
                avatar_url = self.files_service.get_file_url(as_uuid(avatar_file_id))
                logger.debug(f"Generated avatar URL for user {user_id_str} from database: {avatar_url[:50] if avatar_url else 'None'}...")
            except Exception as e:
                logger.warning(f"Failed to get avatar URL for user {user_id_str} from database (file_id: {avatar_file_id}): {e}")
                avatar_url = None
        else:
            logger.debug(f"User {user_id_str} has no avatar_file_id in database")
        
        user_data = {
            'id': profile['user_id'],
            'display_name': profile['display_name'],
            'email': profile['email'],
            'avatar_url': avatar_url,
        }
        
        # Cache the user data (include email for consistency)
        user_data_for_cache = {
            'id': profile['user_id'],
            'display_name': profile['display_name'],
            'email': profile.get('email'),
            'avatar_file_id': profile.get('avatar_file_id'),
        }
        UserCache.set_user(str(user_id), user_data_for_cache)
        
        return user_data
    
    def _batch_get_user_info(self, user_ids: List[UUID4]) -> Dict[str, Dict[str, Any]]:
        """Batch fetch user info - Uses UserCache for caching."""
        if not user_ids:
            return {}
        
        users_dict = {}
        uncached_user_ids = []
        
        # Check cache for each user
        for user_id in user_ids:
            user_id_str = str(user_id)
            cached_user = UserCache.get_user(user_id_str)
            if cached_user:
                # Validate cached data has required keys (handle both 'id' and 'user_id')
                has_id = isinstance(cached_user, dict) and ('id' in cached_user or 'user_id' in cached_user) and 'display_name' in cached_user
                if has_id:
                    avatar_url = None
                    if cached_user.get('avatar_file_id'):
                        try:
                            avatar_url = self.files_service.get_file_url(as_uuid(cached_user['avatar_file_id']))
                        except Exception as e:
                            logger.warning(f"Failed to get avatar URL for user {user_id_str} from batch cache: {e}")
                            avatar_url = None
                    
                    # Handle both 'id' and 'user_id' keys for cache compatibility
                    user_id_value = cached_user.get('id') or cached_user.get('user_id') or user_id_str
                    
                    users_dict[user_id_str] = {
                        'id': user_id_value,
                        'display_name': cached_user.get('display_name', ''),
                        'email': cached_user.get('email'),
                        'avatar_url': avatar_url,
                    }
                else:
                    # Invalid cache structure, fetch from DB
                    uncached_user_ids.append(user_id)
            else:
                uncached_user_ids.append(user_id)
        
        # Batch fetch uncached users
        if uncached_user_ids:
            user_id_strings = [
                str(uid) if isinstance(uid, UUID) else uid for uid in uncached_user_ids
            ]
            uuids = [UUID(x) for x in user_id_strings]
            db = SyncSessionLocal()
            try:
                rows = db.execute(
                    select(
                        Profile.user_id,
                        Profile.display_name,
                        Profile.email,
                        Profile.avatar_file_id,
                    ).where(Profile.user_id.in_(uuids))
                ).all()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to batch get user info: {e}",
                )
            finally:
                db.close()

            found_user_ids = set()
            for profile in [
                {
                    "user_id": r[0],
                    "display_name": r[1],
                    "email": r[2],
                    "avatar_file_id": r[3],
                }
                for r in rows
            ]:
                user_id_str = str(profile['user_id'])
                avatar_url = None
                if profile.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(as_uuid(profile['avatar_file_id']))
                        logger.debug(f"Generated avatar URL for user {user_id_str}: {avatar_url[:50] if avatar_url else 'None'}...")
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str} from batch database: {e}")
                        avatar_url = None
                else:
                    logger.debug(f"User {user_id_str} has no avatar_file_id")
                found_user_ids.add(user_id_str)
                users_dict[user_id_str] = {
                    'id': profile['user_id'],
                    'display_name': profile['display_name'],
                    'email': profile['email'],
                    'avatar_url': avatar_url,
                }
                
                # Cache the user data (include email for consistency)
                user_data_for_cache = {
                    'id': profile['user_id'],
                    'display_name': profile['display_name'],
                    'email': profile.get('email'),
                    'avatar_file_id': profile.get('avatar_file_id'),
                }
                UserCache.set_user(user_id_str, user_data_for_cache)
            
            # Log if some users were not found in profiles table
            missing_user_ids = set(str(uid) for uid in uncached_user_ids) - found_user_ids
            if missing_user_ids:
                logger.warning(f"Users not found in profiles table: {missing_user_ids}")
        
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
            project_id_strings = [
                str(pid) if isinstance(pid, uuid.UUID) else pid
                for pid in uncached_project_ids
            ]
            puuids = [UUID(x) for x in project_id_strings]
            db = SyncSessionLocal()
            try:
                prows = db.execute(
                    select(
                        Project.id,
                        Project.name,
                        Project.avatar_color,
                        Project.avatar_icon,
                        Project.avatar_file_id,
                    ).where(Project.id.in_(puuids))
                ).all()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to batch get project info: {e}",
                )
            finally:
                db.close()

            cache_mapping = {}

            for project in [
                {
                    "id": r[0],
                    "name": r[1],
                    "avatar_color": r[2],
                    "avatar_icon": r[3],
                    "avatar_file_id": r[4],
                }
                for r in prows
            ]:
                avatar_url = None
                if project.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(as_uuid(project['avatar_file_id']))
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
        db = SyncSessionLocal()
        try:
            m = db.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.org_id == UUID(str(org_id)),
                    OrganizationMember.user_id == UUID(str(user_id)),
                )
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get organization member: {e}",
            )
        finally:
            db.close()

        if not m:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User is not a member of this organization",
            )

        return {
            "id": str(m.id),
            "org_id": str(m.org_id),
            "user_id": str(m.user_id),
            "role": m.role,
            "active": m.active,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        }
    
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
