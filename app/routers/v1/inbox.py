from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import Any, Optional
from pydantic import UUID4

from app.schemas.inbox import (
    InboxResponse,
    InboxGetResponse,
    InboxGetPaginatedResponse,
    InboxMarkReadRequest,
    InboxMarkReadResponse,
    InboxArchiveRequest,
    InboxArchiveResponse,
    InboxUnarchiveRequest,
    InboxUnarchiveResponse,
    InboxDeleteRequest,
    InboxDeleteResponse,
)
from app.services.inbox import InboxService
from app.routers.deps import get_current_user, get_active_organization

router = APIRouter()


@router.get('/unread-count', response_model=dict, status_code=status.HTTP_200_OK)
def get_unread_count(
    organization: Any = Depends(get_active_organization),
):
    inbox_service = InboxService()
    count = inbox_service.get_unread_count(UUID4(organization['member_user_id']), UUID4(organization['id']))
    return {
        "unread_count": count
    }


@router.get('/', response_model=InboxGetPaginatedResponse, status_code=status.HTTP_200_OK)
def get_all_inbox(
    include_archived: bool = Query(False, description="Include archived messages"),
    limit: Optional[int] = Query(50, ge=1, le=100, description="Number of items per page"),
    offset: Optional[int] = Query(0, ge=0, description="Number of items to skip"),
    organization: Any = Depends(get_active_organization),
):
    inbox_service = InboxService()
    return inbox_service.get_all_inbox(
        user_id=UUID4(organization['member_user_id']),
        org_id=UUID4(organization['id']),
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )


@router.get('/{inbox_id}', response_model=InboxGetResponse, status_code=status.HTTP_200_OK)
def get_inbox(
    inbox_id: UUID4,
    organization: Any = Depends(get_active_organization),
):
    inbox_service = InboxService()
    return inbox_service.get_inbox(inbox_id, UUID4(organization['member_user_id']))


@router.patch('/{inbox_id}/read', response_model=InboxMarkReadResponse, status_code=status.HTTP_200_OK)
def mark_inbox_read(
    inbox_id: UUID4,
    organization: Any = Depends(get_active_organization),
):
    inbox_service = InboxService()
    return inbox_service.mark_read(inbox_id, UUID4(organization['member_user_id']))


@router.patch('/{inbox_id}/archive', response_model=InboxArchiveResponse, status_code=status.HTTP_200_OK)
def archive_inbox(
    inbox_id: UUID4,
    organization: Any = Depends(get_active_organization),
):
    inbox_service = InboxService()
    return inbox_service.archive_inbox(inbox_id, UUID4(organization['member_user_id']))


@router.patch('/{inbox_id}/unarchive', response_model=InboxUnarchiveResponse, status_code=status.HTTP_200_OK)
def unarchive_inbox(
    inbox_id: UUID4,
    organization: Any = Depends(get_active_organization),
):
    """
    Restore an archived inbox message
    """
    inbox_service = InboxService()
    return inbox_service.unarchive_inbox(inbox_id, UUID4(organization['member_user_id']))


@router.delete('/{inbox_id}', response_model=InboxDeleteResponse, status_code=status.HTTP_200_OK)
def delete_inbox(
    inbox_id: UUID4,
    organization: Any = Depends(get_active_organization),
):
    inbox_service = InboxService()
    return inbox_service.delete_inbox(inbox_id, UUID4(organization['member_user_id']))

