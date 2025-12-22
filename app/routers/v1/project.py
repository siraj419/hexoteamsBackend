from fastapi import APIRouter, Depends, Query, File, UploadFile, status, HTTPException
from pydantic import UUID4
from typing import List, Optional

router = APIRouter()

from app.schemas.projects import (
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectUpdateRequest,
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
    
    try:
        response = supabase.table('project_members').select('*').eq('project_id', str(project_id)).eq('user_id', user.id).execute()
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get project member: {e}"
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
    
    try:
        response = supabase.table('project_members').select('*').eq('project_id', str(project_id)).eq('user_id', user.id).execute()
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check project membership: {e}"
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
    
    # Check if user is a project member
    try:
        member_response = supabase.table('project_members').select('*').eq('project_id', str(project_id)).eq('user_id', user.id).execute()
        if member_response.data and len(member_response.data) > 0:
            return {
                'has_access': True,
                'is_member': True,
                'member_data': member_response.data[0]
            }
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check project membership: {e}"
        )
    
    # Check if user is org admin/owner
    org_member_role = active_organization.get('member_role')
    if org_member_role in [OrganizationMemberRole.ADMIN.value, OrganizationMemberRole.OWNER.value]:
        # Verify project belongs to the organization
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
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify project organization: {e}"
            )
    
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User does not have access to this project"
    )

def _get_user_timezone(user_id: UUID4) -> str:
    from app.core import supabase
    from supabase_auth.errors import AuthApiError
    
    try:
        response = supabase.table('profiles').select('timezone').eq('user_id', str(user_id)).execute()
    except AuthApiError as e:
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
        Get all projects in which the user is a member or and favourite projects
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
    project_service = ProjectService()
    return project_service.get_project(project_id)

@router.post("/{project_id}/members", response_model=ProjectMember, status_code=status.HTTP_201_CREATED)
def add_project_member(
    project_id: UUID4,
    member_request: ProjectAddMemberRequest,
    owner_or_admin: any = Depends(get_project_owner_or_admin),
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
        role=member_request.role
    )

# @router.put("/{project_id}", response_model=ProjectUpdateResponse, status_code=status.HTTP_200_OK)
# def update_project(
#     project_id: UUID4,
#     project_request: ProjectUpdateRequest,
#     user: any = Depends(get_organization_admin_or_owner),
# ):
#     """
#         Update a project by its ID
#     """
#     project_service = ProjectService()
#     return project_service.update_project(project_id, project_request)

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
        - Top 5 latest project attachments
        - Top 5 latest project links
        - Task summary (completed, incomplete, overdue)
        - Team workload (task assigned percentage, per user percentage, unassigned percentage)
        - Top 10 recent activities
    
    Requires: Project member
    Cached: 5 minutes
    """
    project_service = ProjectService()
    return project_service.get_project_summary(project_id)