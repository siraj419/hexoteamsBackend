from fastapi import Request, HTTPException, status, Depends, Query
from supabase_auth.errors import AuthApiError
from pydantic import UUID4
from typing import Any

from app.core import supabase
from app.schemas.organizations import OrganizationMemberRole

def get_current_user(request: Request) -> any:
    
    if not request.headers.get("Authorization"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized, provide a valid token"
        )
    
    # Get session info
    token = request.headers.get("Authorization").split(" ")[1]
    
    # get the user
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized, provide a valid token"
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unauthorized, provide a valid token: {e}"
        )
    
    return user_response.user

def get_active_organization(user: any = Depends(get_current_user)) -> any:
    try:
        response_organization_member = (
            supabase
                .table('organization_members')
                .select('role, organizations(id, name, description, avatar_color, avatar_icon, avatar_file_id)')
                .eq('user_id', user.id)
                .eq('active', True)
                .execute()
        )
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get active organization: {e}"
        )
    
    if not response_organization_member.data or len(response_organization_member.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have an active organization"
        )
    
    return {
        'id': response_organization_member.data[0]['organizations']['id'],
        'name': response_organization_member.data[0]['organizations']['name'],
        'description': response_organization_member.data[0]['organizations']['description'],
        'avatar_color': response_organization_member.data[0]['organizations']['avatar_color'],
        'avatar_icon': response_organization_member.data[0]['organizations']['avatar_icon'],
        'avatar_file_id': response_organization_member.data[0]['organizations']['avatar_file_id'],
        'member_user_id': user.id,
        'member_role': response_organization_member.data[0]['role'],
    }

def get_organization_member(organization_id: UUID4, user: any= Depends(get_current_user)) -> any:
    try:
        response = supabase.table('organization_members').select('*').eq('org_id', organization_id).eq('user_id', user.id).execute()
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get organization member: {e}"
        )
    
    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this organization or the organization does not exist"
        )
    
    return response.data[0]

def get_organization_owner(user: any= Depends(get_current_user)) -> any:
    organization = get_active_organization(user)
    if organization['member_role'] != OrganizationMemberRole.OWNER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not the owner of this organization"
        )
    
    return organization

def get_organization_admin_or_owner(user: any= Depends(get_current_user)) -> any:
    organization = get_active_organization(user)
    member_role = organization['member_role']
    if member_role != OrganizationMemberRole.ADMIN.value and member_role != OrganizationMemberRole.OWNER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not an admin or owner of this organization"
        )
    
    return organization

def get_project_member(project_id: UUID4 = Query(...), user: any= Depends(get_current_user)):
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
    
    member_data = response.data[0]
    member_data['project_id'] = project_id
    member_data['user_id'] = user.id
    return member_data


def get_project_member_with_chat_access(project_id: UUID4, user: any = Depends(get_current_user)) -> dict:
    """
    Verify user is a project member and has chat access
    """
    try:
        response = supabase.table('project_members').select('*').eq(
            'project_id', str(project_id)
        ).eq('user_id', user.id).execute()
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify project membership: {e}"
        )
    
    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this project"
        )
    
    member = response.data[0]
    return {
        'user_id': user.id,
        'project_id': str(project_id),
        'role': member.get('role'),
        'is_admin': member.get('role') in ['admin', 'owner']
    }


def get_dm_conversation_participant(conversation_id: UUID4, user: any = Depends(get_current_user)) -> dict:
    """
    Verify user is part of the DM conversation
    """
    try:
        response = supabase.table('chat_conversations').select('*').eq(
            'id', str(conversation_id)
        ).execute()
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify conversation access: {e}"
        )
    
    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found"
        )
    
    conversation = response.data[0]
    
    if conversation['user1_id'] != user.id and conversation['user2_id'] != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this conversation"
        )
    
    return {
        'user_id': user.id,
        'conversation_id': str(conversation_id),
        'conversation': conversation
    }


def verify_message_author(message_id: UUID4, user: any = Depends(get_current_user), is_project_message: bool = True) -> dict:
    """
    Verify user is the message author (for edit/delete)
    """
    table_name = 'chat_messages' if is_project_message else 'direct_messages'
    user_field = 'user_id' if is_project_message else 'sender_id'
    
    try:
        response = supabase.table(table_name).select('*').eq(
            'id', str(message_id)
        ).execute()
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify message authorship: {e}"
        )
    
    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found"
        )
    
    message = response.data[0]
    
    if message[user_field] != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify your own messages"
        )
    
    return {
        'user_id': user.id,
        'message_id': str(message_id),
        'message': message
    }


def verify_organization_membership(organization_id: UUID4, user: any = Depends(get_current_user)) -> dict:
    """
    Verify user belongs to organization
    """
    try:
        response = supabase.table('organization_members').select('*').eq(
            'organization_id', str(organization_id)
        ).eq('user_id', user.id).execute()
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify organization membership: {e}"
        )
    
    if not response.data or len(response.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this organization"
        )
    
    member = response.data[0]
    return {
        'user_id': user.id,
        'organization_id': str(organization_id),
        'role': member.get('role')
    }


def verify_task_delete_permission(
    task_id: UUID4,
    project_id: UUID4 = Query(...),
    user: any = Depends(get_current_user)
) -> dict:
    """
    Verify user can delete the task:
    - User is the task creator, OR
    - User is organization owner/admin
    
    Requires project_id as query param to verify project membership
    """
    # First verify user is a project member
    try:
        member_response = supabase.table('project_members').select('*').eq(
            'project_id', str(project_id)
        ).eq('user_id', user.id).execute()
        
        if not member_response.data or len(member_response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not a member of this project"
            )
    except HTTPException:
        raise
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify project membership: {e}"
        )
    
    # Get task information and project's org_id
    try:
        task_response = supabase.table('tasks').select('created_by, project_id').eq(
            'id', str(task_id)
        ).execute()
        
        if not task_response.data or len(task_response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )
        
        task = task_response.data[0]
        task_creator_id = task.get('created_by')
        task_project_id = task.get('project_id')
        
        # Verify task belongs to the specified project
        if str(task_project_id) != str(project_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Task does not belong to the specified project"
            )
        
    except HTTPException:
        raise
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get task information: {e}"
        )
    
    # Check if user is the task creator
    if str(task_creator_id) == str(user.id):
        return {
            'user_id': user.id,
            'task_id': str(task_id),
            'can_delete': True,
            'is_creator': True,
            'is_org_admin': False
        }
    
    # Check if user is organization admin/owner
    try:
        project_response = supabase.table('projects').select('org_id').eq(
            'id', str(project_id)
        ).execute()
        
        if not project_response.data or len(project_response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
        
        org_id = project_response.data[0]['org_id']
        
        org_member_response = supabase.table('organization_members').select('role').eq(
            'org_id', str(org_id)
        ).eq('user_id', user.id).execute()
        
        if org_member_response.data and len(org_member_response.data) > 0:
            user_role = org_member_response.data[0].get('role')
            
            if user_role in [OrganizationMemberRole.OWNER.value, OrganizationMemberRole.ADMIN.value]:
                return {
                    'user_id': user.id,
                    'task_id': str(task_id),
                    'can_delete': True,
                    'is_creator': False,
                    'is_org_admin': True
                }
    except HTTPException:
        raise
    except AuthApiError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify organization role: {e}"
        )
    
    # User doesn't have permission
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the task creator or organization admins/owners can delete this task"
    )
 