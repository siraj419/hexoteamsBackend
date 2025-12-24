from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File
from pydantic import UUID4
from typing import List, Optional
from datetime import datetime

from app.routers.deps import (
    get_current_user,
    get_active_organization,
    get_project_member_with_chat_access,
    get_dm_conversation_participant,
    verify_organization_membership,
)
from app.schemas.chat import (
    ProjectMessageCreate,
    ProjectMessageResponse,
    ProjectMessageUpdate,
    DirectMessageCreate,
    DirectMessageResponse,
    DirectMessageListResponse,
    DirectMessageUpdate,
    MessageReadRequest,
    TypingIndicatorRequest,
    ConversationCreate,
    ConversationResponse,
    ConversationListResponse,
    NotificationSummaryResponse,
    AttachmentUploadResponse,
    AttachmentDownloadResponse,
    ProjectConversationListResponse,
)
from app.services.chat import ChatService
from app.services.files import FilesService
from app.utils.websocket_manager import manager
from app.core import supabase

router = APIRouter()


@router.post('/projects/{project_id}/messages', response_model=ProjectMessageResponse, status_code=status.HTTP_201_CREATED)
async def send_project_message(
    project_id: UUID4,
    message_data: ProjectMessageCreate,
    project_member: any = Depends(get_project_member_with_chat_access),
):
    """
    Send a message to project chat
    
    Requires: Project member
    """
    
    chat_service = ChatService()
    response = chat_service.send_project_message(
        project_id,
        UUID4(project_member['user_id']),
        message_data
    )
    
    await manager.broadcast_to_project(
        str(project_id), 
        {
            "type": "message",
            "data": response.model_dump(mode='json')
        },
        sender_id=str(project_member['user_id'])
    )
    
    return response


@router.get('/projects/{project_id}/messages', status_code=status.HTTP_200_OK)
def get_project_messages(
    project_id: UUID4,
    project_member: any = Depends(get_project_member_with_chat_access),
    limit: Optional[int] = Query(50, ge=1, le=100),
    offset: Optional[int] = Query(0, ge=0),
    before_date: Optional[datetime] = Query(None),
    after_date: Optional[datetime] = Query(None),
    search: Optional[str] = Query(None),
):
    """
    Get paginated message history for project chat
    
    Requires: Project member
    """

    chat_service = ChatService()
    return chat_service.get_project_messages(
        project_id,
        limit=limit,
        offset=offset,
        before_date=before_date,
        after_date=after_date,
        search=search
    )


@router.patch('/projects/{project_id}/messages/{message_id}', response_model=ProjectMessageResponse, status_code=status.HTTP_200_OK)
async def edit_project_message(
    project_id: UUID4,
    message_id: UUID4,
    message_data: ProjectMessageUpdate,
    project_member: any = Depends(get_project_member_with_chat_access),
):
    """
    Edit a project message (only by author, within 24 hours)
    
    Requires: Message author
    """
    
    chat_service = ChatService()
    updated_message = chat_service.edit_message(
        message_id,
        UUID4(project_member['user_id']),
        message_data,
        is_project_message=True
    )
    
    response = ProjectMessageResponse(**updated_message)
    
    await manager.broadcast_to_project(
        str(project_id),
        {
            "type": "message_edited",
            "data": response.model_dump(mode='json')
        },
        sender_id=str(project_member['user_id'])
    )
    
    return response


@router.delete('/projects/{project_id}/messages/{message_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_message(
    project_id: UUID4,
    message_id: UUID4,
    project_member: any = Depends(get_project_member_with_chat_access),
):
    """
    Soft delete a project message
    
    Requires: Message author or project admin
    """
    
    chat_service = ChatService()
    chat_service.delete_message(
        message_id,
        UUID4(project_member['user_id']),
        is_project_admin=project_member['is_admin'],
        is_project_message=True
    )
    
    await manager.broadcast_to_project(
        str(project_id),
        {
            "type": "message_deleted",
            "message_id": str(message_id)
        },
        sender_id=str(project_member['user_id'])
    )
    
    return None


@router.post('/projects/{project_id}/typing', status_code=status.HTTP_204_NO_CONTENT)
def send_project_typing_indicator(
    project_id: UUID4,
    typing_data: TypingIndicatorRequest,
    user: any = Depends(get_current_user),
):
    """
    Send typing indicator to project chat
    
    Requires: Project member
    """
    project_member = get_project_member_with_chat_access(project_id, user)
    
    chat_service = ChatService()
    chat_service.send_typing_indicator(
        project_id,
        UUID4(user.id),
        typing_data.is_typing,
        chat_type='project'
    )
    
    return None


@router.post('/projects/{project_id}/read', status_code=status.HTTP_204_NO_CONTENT)
async def mark_project_messages_read(
    project_id: UUID4,
    read_data: MessageReadRequest,
    project_member: any = Depends(get_project_member_with_chat_access),
):
    """
    Mark project messages as read
    
    Requires: Project member
    """
    
    chat_service = ChatService()
    marked_message_ids = chat_service.mark_project_messages_read(
        project_id,
        UUID4(project_member['user_id']),
        read_data.last_read_message_id
    )
    
    # Broadcast all messages that were marked as read
    if marked_message_ids:
        await manager.broadcast_to_project(
            str(project_id),
            {
                "type": "read",
                "user_id": str(project_member['user_id']),
                "message_ids": marked_message_ids,
                "last_read_message_id": str(read_data.last_read_message_id)
            },
            sender_id=str(project_member['user_id'])
        )
    
    return None


@router.get('/projects/conversations', response_model=ProjectConversationListResponse, status_code=status.HTTP_200_OK)
def get_project_conversations(
    organization: any = Depends(get_active_organization),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
    Get list of all project conversations for current user
    
    Returns all projects where the user is a member, with:
    - Last message preview
    - Unread count
    - Project info (name, avatar, etc.)
    - Ordered by last message time (newest first)
    
    Requires: Organization member
    """
    
    chat_service = ChatService()
    result = chat_service.get_project_conversations(
        UUID4(organization['member_user_id']),
        organization['id'],
        limit=limit,
        offset=offset
    )
    
    return ProjectConversationListResponse(**result)


@router.get('/direct/conversations', response_model=ConversationListResponse, status_code=status.HTTP_200_OK)
def get_dm_conversations(
    organization: any = Depends(get_active_organization),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
    Get list of all DM conversations for current user
    
    Requires: Organization member
    """
    
    chat_service = ChatService()
    result = chat_service.get_dm_conversations(
        UUID4(organization['member_user_id']),
        organization['id'],
        limit=limit,
        offset=offset
    )
    
    return ConversationListResponse(**result)


@router.post('/direct/conversations', response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def create_dm_conversation(
    conversation_data: ConversationCreate,
    organization: any = Depends(get_active_organization),
):
    """
    Start a new DM conversation
    
    Requires: Active organization
    Validation: Both users must be in same organization
    """
        
    chat_service = ChatService()
    return chat_service.create_dm_conversation(
        UUID4(organization['member_user_id']),
        conversation_data.receiver_id,
        organization['id']
    )


@router.get('/direct/conversations/{conversation_id}/messages', response_model=DirectMessageListResponse, status_code=status.HTTP_200_OK)
def get_dm_messages(
    conversation_id: UUID4,
    conversation_participant: any = Depends(get_dm_conversation_participant),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
    before_date: Optional[datetime] = Query(None),
    after_date: Optional[datetime] = Query(None),
):
    """
    Get paginated message history for DM conversation
    
    Requires: Conversation participant
    """
    
    chat_service = ChatService()
    result = chat_service.get_direct_messages(
        conversation_id,
        UUID4(conversation_participant['user_id']),
        limit=limit,
        offset=offset,
        before_date=before_date,
        after_date=after_date
    )
    
    return DirectMessageListResponse(**result)


@router.post('/direct/conversations/{conversation_id}/messages', response_model=DirectMessageResponse, status_code=status.HTTP_201_CREATED)
async def send_direct_message(
    conversation_id: UUID4,
    message_data: DirectMessageCreate,
    conversation_participant: any = Depends(get_dm_conversation_participant),
):
    """
    Send a direct message
    
    Requires: Conversation participant
    """
    
    chat_service = ChatService()
    response = chat_service.send_direct_message(
        conversation_id,
        UUID4(conversation_participant['user_id']),
        message_data
    )
    
    await manager.broadcast_to_dm(
        str(conversation_id), 
        {
            "type": "message",
            "data": response.model_dump(mode='json')
        },
        sender_id=str(conversation_participant['user_id'])
    )
    
    return response


@router.patch('/direct/conversations/{conversation_id}/messages/{message_id}', response_model=DirectMessageResponse, status_code=status.HTTP_200_OK)
async def edit_direct_message(
    message_id: UUID4,
    conversation_id: UUID4,
    message_data: DirectMessageUpdate,
    conversation_participant: any = Depends(get_dm_conversation_participant),
):
    """
    Edit a direct message (only by sender, within 24 hours)
    
    Requires: Message sender
    """
    chat_service = ChatService()
    updated_message = chat_service.edit_message(
        message_id,
        UUID4(conversation_participant['user_id']),
        message_data,
        is_project_message=False
    )
    
    response = DirectMessageResponse(**updated_message)
    
    if conversation_id:
        await manager.broadcast_to_dm(
            str(conversation_id),
            {
                "type": "message_edited",
                "data": response.model_dump(mode='json')
            },
            sender_id=str(conversation_participant['user_id'])
        )
    
    return response


@router.delete('/direct/messages/{message_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_direct_message(
    message_id: UUID4,
    user: any = Depends(get_current_user),
):
    """
    Soft delete a direct message (only by sender)
    
    Requires: Message sender
    """
    chat_service = ChatService()
    chat_service.delete_message(
        message_id,
        UUID4(user.id),
        is_project_admin=False,
        is_project_message=False
    )
    
    # Get conversation_id from the message
    message_response = supabase.table('direct_messages').select('sender_id, receiver_id, organization_id').eq('id', str(message_id)).execute()
    conversation_id = None
    if message_response.data:
        sender_id = message_response.data[0]['sender_id']
        receiver_id = message_response.data[0]['receiver_id']
        organization_id = message_response.data[0]['organization_id']
        
        # Find the conversation
        user1_id = min(sender_id, receiver_id)
        user2_id = max(sender_id, receiver_id)
        conv_response = supabase.table('chat_conversations').select('id').eq(
            'user1_id', user1_id
        ).eq('user2_id', user2_id).eq('organization_id', organization_id).execute()
        
        if conv_response.data:
            conversation_id = conv_response.data[0]['id']
    
    if conversation_id:
        await manager.broadcast_to_dm(
            str(conversation_id),
            {
                "type": "message_deleted",
                "message_id": str(message_id)
            },
            sender_id=str(user.id)
        )
    
    return None


@router.post('/direct/conversations/{conversation_id}/typing', status_code=status.HTTP_204_NO_CONTENT)
async def send_dm_typing_indicator(
    conversation_id: UUID4,
    typing_data: TypingIndicatorRequest,
    conversation_participant: any = Depends(get_dm_conversation_participant),
):
    """
    Send typing indicator to DM conversation
    
    Requires: Conversation participant
    """

    
    chat_service = ChatService()
    chat_service.send_typing_indicator(
        conversation_id,
        UUID4(conversation_participant['user_id']),
        typing_data.is_typing,
        chat_type='direct'
    )
    
    # Broadcast typing indicator to the conversation
    await manager.broadcast_to_dm(
        str(conversation_id),
        {
            "type": "typing",
            "is_typing": typing_data.is_typing 
        },
        sender_id=str(conversation_participant['user_id'])
    )
    
    return None


@router.post('/direct/conversations/{conversation_id}/read', status_code=status.HTTP_204_NO_CONTENT)
async def mark_dm_messages_read(
    conversation_id: UUID4,
    read_data: MessageReadRequest,
    user: any = Depends(get_current_user),
):
    """
    Mark direct messages as read
    
    Requires: Conversation participant
    """
    conversation_participant = get_dm_conversation_participant(conversation_id, user)
    
    chat_service = ChatService()
    marked_message_ids = chat_service.mark_dm_read(
        conversation_id,
        UUID4(user.id),
        read_data.last_read_message_id
    )
    
    # Broadcast all messages that were marked as read
    if marked_message_ids:
        await manager.broadcast_to_dm(
            str(conversation_id),
            {
                "type": "read",
                "user_id": str(user.id),
                "message_ids": marked_message_ids,
                "last_read_message_id": str(read_data.last_read_message_id)
            },
            sender_id=str(user.id)
        )
    
    return None


@router.post('/attachments/upload', response_model=AttachmentUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_chat_attachment(
    file: UploadFile = File(...),
    chat_type: str = Query(..., regex="^(project|direct)$"),
    reference_id: UUID4 = Query(...),
    organization: any = Depends(get_active_organization),
):
    """
    Upload attachment before sending message
    
    Requires: Organization member
    Validation: File size limits, type restrictions
    """
    
    files_service = FilesService()
    
    MAX_FILE_SIZE = 100 * 1024 * 1024
    file_content = await file.read()
    
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds maximum limit of {MAX_FILE_SIZE} bytes"
        )
    
    try:
        result = files_service.upload_chat_attachment(
            file_content,
            file.filename,
            file.content_type,
            UUID4(organization['member_user_id']),
            organization['id'],
            chat_type,
            reference_id
        )
        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload attachment: {str(e)}"
        )


@router.get('/attachments/{attachment_id}/download', response_model=AttachmentDownloadResponse, status_code=status.HTTP_200_OK)
def get_attachment_download_url(
    attachment_id: UUID4,
    orgnization: any = Depends(get_active_organization),
):
    """
    Get signed URL for attachment download
    
    Requires: Message participant
    Security: Verify user has access to parent message
    """
    files_service = FilesService()
    
    try:
        result = files_service.get_chat_attachment_download_url(
            attachment_id,
            UUID4(orgnization['member_user_id'])
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get download URL: {str(e)}"
        )


@router.get('/search', status_code=status.HTTP_200_OK)
def search_messages(
    q: str = Query(..., min_length=1, description="Search term"),
    chat_type: Optional[str] = Query(None, regex="^(project|direct)$"),
    organization: any = Depends(get_active_organization),
    limit: Optional[int] = Query(None, ge=1),
    offset: Optional[int] = Query(None, ge=0),
):
    """
    Search across all accessible chats
    
    Requires: Organization member
    Searches: Message body, file names
    Filters: By project, by user, by date range
    """
    
    chat_service = ChatService()
    return chat_service.search_messages(
        UUID4(organization['member_user_id']),
        organization['id'],
        q,
        chat_type=chat_type,
        limit=limit,
        offset=offset
    )


@router.get('/notifications/summary', response_model=NotificationSummaryResponse, status_code=status.HTTP_200_OK)
def get_notification_summary(
    organization: any = Depends(get_active_organization),
):
    """
    Get unread message summary
    
    Requires: Organization member
    """
    
    chat_service = ChatService()
    return chat_service.get_unread_summary(
        UUID4(organization['member_user_id']),
        organization['id']
    )
