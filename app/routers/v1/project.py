from fastapi import APIRouter, Depends, Query, File, UploadFile, status, HTTPException, Form
from pydantic import UUID4
from typing import List, Optional
from datetime import date
import httpx
import time
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

from app.schemas.projects import (
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectUpdateRequest,
    ProjectUpdateOptimizedRequest,
    ProjectUpdateResponse,
    ProjectGetResponse,
    ProjectGetPaginatedResponse,
    AllProjectsResponse,
    ProjectChangeAvatarResponse,
    ProjectMemberRole,
    ProjectMember,
    ProjectOrderBy,
    NonMemberProjectsResponse,
    ArchivedProjectsResponse,
    ProjectSummaryResponse,
    ProjectAddMemberRequest,
    FavouriteProjectsResponse,
    RecentProjectsResponse,
    ProjectMembersResponse,
)
from app.schemas.organizations import OrganizationMemberRole

from app.services.project import ProjectService
from app.services.attachment import AttachmentService
from app.services.files import FilesService
from app.services.link import LinkService
from app.services.activity import ActivityService, ActivityType
from app.routers.deps import get_active_organization, get_current_user
from app.schemas.activities import ActivityGetPaginatedResponse
from app.schemas.attachments import (
    AttachmentType,
    AttachmentRequest,
    AttachmentResponse,
    AttachmentGetPaginatedResponse,
)
from app.schemas.links import (
    LinkEntityType,
    LinkRequest,
    LinkResponse,
    LinkUpdateRequest,
    LinkGetPaginatedResponse,
)

def check_organization_admin_or_owner(active_organization: any) -> None:
    if active_organization['member_role'] != OrganizationMemberRole.ADMIN.value and active_organization['member_role'] != OrganizationMemberRole.OWNER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not an admin or owner of this organization"
        )

def get_project_member_for_attachments(project_id: UUID4, user: any = Depends(get_current_user)):
    from app.core import supabase
    from supabase_auth.errors import AuthApiError
    
    max_retries = 3
    retry_delay = 0.5
    
    for attempt in range(max_retries):
        try:
            response = supabase.table('project_members').select('*').eq('project_id', str(project_id)).eq('user_id', user.id).execute()
            break
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                logger.warning(f"Network error getting project member (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"Failed to get project member after {max_retries} attempts: {e}")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service temporarily unavailable. Please try again in a moment."
                )
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get project member: {e}"
            )
        except Exception as e:
            logger.error(f"Unexpected error getting project member: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get project member: {str(e)}"
            )
    
    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this project"
        )
    
    return response.data[0]

def get_project_owner_or_admin(project_id: UUID4, user: any = Depends(get_current_user)):
    """
    Check if user is project owner or admin.
    Only project owners and admins can add members to a project.
    """
    from app.core import supabase
    from supabase_auth.errors import AuthApiError
    
    max_retries = 3
    retry_delay = 0.5
    
    for attempt in range(max_retries):
        try:
            response = supabase.table('project_members').select('*').eq('project_id', str(project_id)).eq('user_id', user.id).execute()
            break
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                logger.warning(f"Network error checking project membership (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"Failed to check project membership after {max_retries} attempts: {e}")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service temporarily unavailable. Please try again in a moment."
                )
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check project membership: {e}"
            )
        except Exception as e:
            logger.error(f"Unexpected error checking project membership: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check project membership: {str(e)}"
            )
    
    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this project"
        )
    
    member = response.data[0]
    member_role = member.get('role')
    
    if member_role not in [ProjectMemberRole.OWNER.value, ProjectMemberRole.ADMIN.value]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only project owners and admins can add members to a project"
        )
    
    return member

def get_project_access(project_id: UUID4, user: any = Depends(get_current_user), active_organization: any = Depends(get_active_organization)):
    """
    Check if user has access to project:
    - User is a project member, OR
    - User is organization admin/owner
    """
    from app.core import supabase
    from supabase_auth.errors import AuthApiError
    
    max_retries = 3
    retry_delay = 0.5
    
    # Check if user is a project member
    for attempt in range(max_retries):
        try:
            member_response = supabase.table('project_members').select('*').eq('project_id', str(project_id)).eq('user_id', user.id).execute()
            if member_response.data and len(member_response.data) > 0:
                return {
                    'has_access': True,
                    'is_member': True,
                    'member_data': member_response.data[0]
                }
            break
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                logger.warning(f"Network error checking project membership (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"Failed to check project membership after {max_retries} attempts: {e}")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service temporarily unavailable. Please try again in a moment."
                )
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check project membership: {e}"
            )
        except Exception as e:
            logger.error(f"Unexpected error checking project membership: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check project membership: {str(e)}"
            )
    
    # Check if user is org admin/owner
    org_member_role = active_organization.get('member_role')
    if org_member_role in [OrganizationMemberRole.ADMIN.value, OrganizationMemberRole.OWNER.value]:
        # Verify project belongs to the organization
        for attempt in range(max_retries):
            try:
                project_response = supabase.table('projects').select('org_id').eq('id', str(project_id)).execute()
                if project_response.data and len(project_response.data) > 0:
                    project_org_id = project_response.data[0]['org_id']
                    if str(project_org_id) == str(active_organization['id']):
                        return {
                            'has_access': True,
                            'is_member': False,
                            'member_data': None
                        }
                break
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    logger.warning(f"Network error verifying project organization (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Failed to verify project organization after {max_retries} attempts: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Service temporarily unavailable. Please try again in a moment."
                    )
            except AuthApiError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to verify project organization: {e}"
                )
            except Exception as e:
                logger.error(f"Unexpected error verifying project organization: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to verify project organization: {str(e)}"
                )
    
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User does not have access to this project"
    )

def _get_user_timezone(user_id: UUID4) -> str:
    from app.core import supabase
    from supabase_auth.errors import AuthApiError
    
    max_retries = 3
    retry_delay = 0.5
    
    for attempt in range(max_retries):
        try:
            response = supabase.table('profiles').select('timezone').eq('user_id', str(user_id)).execute()
            break
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                logger.warning(f"Network error getting user timezone (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                logger.warning(f"Failed to get user timezone after {max_retries} attempts: {e}. Using UTC as default.")
                return 'UTC'
        except AuthApiError as e:
            logger.warning(f"Auth error getting user timezone: {e}. Using UTC as default.")
            return 'UTC'
        except Exception as e:
            logger.warning(f"Unexpected error getting user timezone: {e}. Using UTC as default.")
            return 'UTC'
    
    if not response.data or len(response.data) == 0:
        return 'UTC'
    
    return response.data[0].get('timezone', 'UTC')

@router.post("/create", response_model=ProjectCreateResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    project_request: ProjectCreateRequest,
    active_organization: any = Depends(get_active_organization)
):
    """
        Create a new project for the active organization
    """
    
    # permission check
    check_organization_admin_or_owner(active_organization)
    
    # create the project
    project_service = ProjectService()
    return project_service.create_project(
        project_request=project_request,
        org_id=active_organization['id'],
        user_id=active_organization['member_user_id']
    )

@router.get("/", response_model=AllProjectsResponse, status_code=status.HTTP_200_OK)
def get_projects(
    active_organization: any = Depends(get_active_organization),
    search: Optional[str] = None,
    order_by: Optional[ProjectOrderBy] = None,
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0)
):
    """
        Get all projects in which the user is a member
    """
    project_service = ProjectService()
    return project_service.get_projects(
        org_id=active_organization['id'],
        user_id=active_organization['member_user_id'],
        org_member_role=active_organization['member_role'],
        search=search,
        order_by=order_by,
        limit=limit,
        offset=offset
    )

@router.get("/favourites", response_model=FavouriteProjectsResponse, status_code=status.HTTP_200_OK)
def get_favourite_projects(
    active_organization: any = Depends(get_active_organization),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0)
):
    """
        Get all favourite projects for the current user in the active organization
    """
    project_service = ProjectService()
    return project_service.get_favourite_projects(
        user_id=active_organization['member_user_id'],
        org_id=active_organization['id'],
        limit=limit,
        offset=offset
    )

@router.get("/recent", response_model=RecentProjectsResponse, status_code=status.HTTP_200_OK)
def get_recent_projects(
    active_organization: any = Depends(get_active_organization),
):
    """
        Get 5 most recent projects (by created_at) that the user is a member of.
        Returns only id and name for sidebar display.
    """
    project_service = ProjectService()
    return project_service.get_recent_projects(
        org_id=active_organization['id'],
        user_id=active_organization['member_user_id'],
    )

@router.get("/non-member", response_model=NonMemberProjectsResponse, status_code=status.HTTP_200_OK)
def get_non_member_projects(
    active_organization: any = Depends(get_active_organization),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0)
):
    """
        Get all projects in which the user is not a member but he is the owner or admin of the organization
    """
    # permission check
    check_organization_admin_or_owner(active_organization)
    
    project_service = ProjectService()
    return project_service.get_non_member_projects(
        org_id=active_organization['id'],
        user_id=active_organization['member_user_id'],
        limit=limit,
        offset=offset
    )

@router.get("/archived", response_model=ArchivedProjectsResponse, status_code=status.HTTP_200_OK)
def get_archived_projects(
    active_organization: any = Depends(get_active_organization),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0)
):
    """
        Get all archived projects for the active organization
    """
    
    # permission check
    check_organization_admin_or_owner(active_organization)
    
    project_service = ProjectService()
    return project_service.get_archived_projects(
        org_id=active_organization['id'],
        user_id=active_organization['member_user_id'],
        limit=limit,
        offset=offset
    )

@router.patch("/{project_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_project(
    project_id: UUID4,
    active_organization: any = Depends(get_active_organization),
):
    """
        Archive a project by its ID
    """
    # permission check
    check_organization_admin_or_owner(active_organization)
    
    project_service = ProjectService()
    return project_service.archive_project(project_id)

@router.patch("/{project_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
def restore_project(
    project_id: UUID4,
    active_organization: any = Depends(get_active_organization),
):
    """
        Restore a project by its ID
    """
    
    # permission check
    check_organization_admin_or_owner(active_organization)
    
    project_service = ProjectService()
    return project_service.restore_project(project_id)

@router.patch('/{project_id}/favourite', status_code=status.HTTP_204_NO_CONTENT)
def toggle_project_favourite(
    project_id: UUID4,
    active_organization: any = Depends(get_active_organization),
):
    """
        Toggle the favourite status of a project by its ID
    """
    
    project_service = ProjectService()
    return project_service.toggle_project_favourite(project_id, user_id=active_organization['member_user_id'])

@router.post("/{project_id}/join", status_code=status.HTTP_204_NO_CONTENT)
def join_project(
    project_id: UUID4,
    active_organization: any = Depends(get_active_organization),
):
    """
        Allow organization owner or admin to join any project without invitation
    """
    check_organization_admin_or_owner(active_organization)
    
    project_service = ProjectService()
    return project_service.join_project(
        project_id=project_id,
        user_id=active_organization['member_user_id'],
        org_id=active_organization['id']
    )

@router.get("/{project_id}", response_model=ProjectGetResponse, status_code=status.HTTP_200_OK)
def get_project(
    project_id: UUID4,
    access: any = Depends(get_project_access),
):
    """
        Get a project by its ID.
        User must be a project member or organization admin/owner.
    """
    from app.routers.deps import get_current_user
    project_service = ProjectService()
    user_id = None
    if access.get('member_data'):
        user_id = UUID4(access['member_data']['user_id'])
    elif access.get('has_access'):
        # Try to get user from current_user dependency
        try:
            from fastapi import Request
            # Get user from request if available
            pass
        except:
            pass
    return project_service.get_project(project_id, user_id=user_id)

@router.get("/{project_id}/members", response_model=ProjectMembersResponse, status_code=status.HTTP_200_OK)
def get_project_members(
    project_id: UUID4,
    member: any = Depends(get_project_member_for_attachments),
):
    """
    Get all members of a project.
    Returns a list of ProjectMemberSummary with id, display_name, and avatar_url.
    Requires: Project member
    """
    project_service = ProjectService()
    members = project_service.get_project_members(project_id)
    return ProjectMembersResponse(members=members)

@router.post("/{project_id}/members", response_model=ProjectMember, status_code=status.HTTP_201_CREATED)
def add_project_member(
    project_id: UUID4,
    member_request: ProjectAddMemberRequest,
    owner_or_admin: any = Depends(get_project_owner_or_admin),
    current_user: any = Depends(get_current_user),
):
    """
        Add a member to a project.
        Only project owners and admins can add members.
        The user being added must be a member of the organization.
    """
    project_service = ProjectService()
    return project_service.add_project_member(
        project_id=project_id,
        user_id=member_request.user_id,
        role=member_request.role,
        added_by_id=UUID4(current_user.id),
    )

@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project_member(
    project_id: UUID4,
    user_id: UUID4,
    owner_or_admin: any = Depends(get_project_owner_or_admin),
    current_user: any = Depends(get_current_user),
):
    """
        Remove a member from a project.
        Only project owners and admins can remove members.
        Cannot remove the last owner of the project.
    """
    # Prevent removing yourself
    if str(user_id) == str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove yourself from the project"
        )
    
    project_service = ProjectService()
    project_service.remove_project_member(
        project_id=project_id,
        user_id=user_id,
        removed_by_id=UUID4(current_user.id),
    )
    return None

@router.patch("/{project_id}", response_model=ProjectUpdateResponse, status_code=status.HTTP_200_OK)
def update_project_optimized(
    project_id: UUID4,
    project_request: ProjectUpdateOptimizedRequest,
    active_organization: any = Depends(get_active_organization),
    project_member: any = Depends(get_project_owner_or_admin),
):
    """
    Optimized endpoint to update project details.
    
    Updates:
    - name: Project name
    - avatar_file_id: Project avatar file ID (UUID of existing file)
    - avatar_color: Project avatar color
    - avatar_icon: Project avatar icon
    - start_date: Project start date
    - end_date: Project end date
    
    Requires: Project owner/admin or organization admin/owner
    Optimized: Single database query, cache invalidation
    """
    # Check if user is org admin/owner (can update any project in org)
    # or project owner/admin (can update their project)
    is_org_admin_or_owner = (
        active_organization['member_role'] == OrganizationMemberRole.ADMIN.value or
        active_organization['member_role'] == OrganizationMemberRole.OWNER.value
    )
    
    if not is_org_admin_or_owner and not project_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only project owners/admins or organization admins/owners can update projects"
        )
    
    # Validate that at least one field is being updated
    if not any([
        project_request.name is not None,
        project_request.avatar_file_id is not None,
        project_request.avatar_color is not None,
        project_request.avatar_icon is not None,
        project_request.start_date is not None,
        project_request.end_date is not None,
    ]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided"
        )
    
    project_service = ProjectService()
    return project_service.update_project_optimized(
        project_id=project_id,
        name=project_request.name,
        avatar_file_id=project_request.avatar_file_id,
        avatar_color=project_request.avatar_color,
        avatar_icon=project_request.avatar_icon,
        start_date=project_request.start_date,
        end_date=project_request.end_date,
    )

@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: UUID4,
    active_organization: any = Depends(get_active_organization),
):
    """
        Delete a project by its ID and all its resources
    """
    
    # permission check
    check_organization_admin_or_owner(active_organization)
    
    # delete the project
    project_service = ProjectService()
    return project_service.delete_project(project_id)

@router.put("/{project_id}/avatar", response_model=ProjectChangeAvatarResponse, status_code=status.HTTP_200_OK)
def change_project_avatar(
    project_id: UUID4,
    file: UploadFile = File(...),
    active_organization: any = Depends(get_active_organization),
):
    """
        Change the avatar of a project by its ID
    """
    # permission check
    check_organization_admin_or_owner(active_organization)
    
    project_service = ProjectService()
    return project_service.change_project_avatar(
        user_id=active_organization['member_user_id'],
        org_id=active_organization['id'],
        project_id=project_id,
        file=file,
    )

@router.post("/{project_id}/attachments", response_model=AttachmentResponse, status_code=status.HTTP_201_CREATED)
def add_project_attachment(
    project_id: UUID4,
    attachment_request: AttachmentRequest,
    member: any = Depends(get_project_member_for_attachments),
):
    """
        Add an attachment to a project
    """
    attachment_service = AttachmentService(files_service=FilesService())
    return attachment_service.add_attachment(AttachmentType.PROJECT, project_id, attachment_request.file_id)

@router.get("/{project_id}/attachments", response_model=AttachmentGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_project_attachments(
    project_id: UUID4,
    member: any = Depends(get_project_member_for_attachments),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
        Get all attachments for a project with pagination
    """
    attachment_service = AttachmentService(files_service=FilesService())
    return attachment_service.get_attachments(AttachmentType.PROJECT, project_id, limit=limit, offset=offset)

@router.delete("/{project_id}/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_attachment(
    project_id: UUID4,
    attachment_id: UUID4,
    member: any = Depends(get_project_member_for_attachments),
):
    """
        Delete a project attachment
    """
    attachment_service = AttachmentService(files_service=FilesService())
    attachment_service.delete_attachment(attachment_id)
    return None

@router.post("/{project_id}/links", response_model=LinkResponse, status_code=status.HTTP_201_CREATED)
def add_project_link(
    project_id: UUID4,
    link_request: LinkRequest,
    member: any = Depends(get_project_member_for_attachments),
):
    """
        Add a link to a project
    """
    user_id = UUID4(member['user_id']) if isinstance(member['user_id'], str) else member['user_id']
    user_timezone = _get_user_timezone(user_id)
    link_service = LinkService(user_timezone)
    return link_service.create_link(link_request, project_id, LinkEntityType.PROJECT)

@router.get("/{project_id}/links", response_model=LinkGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_project_links(
    project_id: UUID4,
    member: any = Depends(get_project_member_for_attachments),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
        Get all links for a project with pagination
    """
    user_id = UUID4(member['user_id']) if isinstance(member['user_id'], str) else member['user_id']
    user_timezone = _get_user_timezone(user_id)
    link_service = LinkService(user_timezone)
    return link_service.get_links(project_id, LinkEntityType.PROJECT, limit=limit, offset=offset)

@router.put("/{project_id}/links/{link_id}", response_model=LinkResponse, status_code=status.HTTP_200_OK)
def update_project_link(
    project_id: UUID4,
    link_id: UUID4,
    link_request: LinkUpdateRequest,
    member: any = Depends(get_project_member_for_attachments),
):
    """
        Update a project link
    """
    user_id = UUID4(member['user_id']) if isinstance(member['user_id'], str) else member['user_id']
    user_timezone = _get_user_timezone(user_id)
    link_service = LinkService(user_timezone)
    return link_service.update_link(link_id, link_request)

@router.delete("/{project_id}/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_link(
    project_id: UUID4,
    link_id: UUID4,
    member: any = Depends(get_project_member_for_attachments),
):
    """
        Delete a project link
    """
    link_service = LinkService()
    link_service.delete_link(link_id)
    return None

@router.get("/{project_id}/activities", response_model=ActivityGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_project_activities(
    project_id: UUID4,
    member: any = Depends(get_project_member_for_attachments),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
        Get all activities for a project with pagination
        Returns both project activities and task activities for tasks in this project
    """
    activity_service = ActivityService(files_service=FilesService())
    return activity_service.get_activities_paginated(
        project_id,
        ActivityType.PROJECT,
        limit=limit,
        offset=offset,
    )

@router.get("/{project_id}/summary", response_model=ProjectSummaryResponse, status_code=status.HTTP_200_OK)
def get_project_summary(
    project_id: UUID4,
    member: any = Depends(get_project_member_for_attachments),
):
    """
    Get comprehensive project summary.
    
    Returns:
        - Project members (id, display_name, avatar_url)
        - Top 5 latest project links
        - Task summary (completed, incomplete, overdue)
        - Team workload (task assigned percentage, per user percentage, unassigned percentage)
        - Top 10 recent activities
    
    Requires: Project member
    Cached: 5 minutes
    """
    project_service = ProjectService()
    user_id = UUID4(member['user_id']) if isinstance(member['user_id'], str) else member['user_id']
    return project_service.get_project_summary(project_id, user_id=user_id)