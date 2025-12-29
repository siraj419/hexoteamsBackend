from fastapi import HTTPException, status
from pydantic import UUID4
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from supabase_auth.errors import AuthApiError
import logging
import json

from app.schemas.inbox import (
    InboxResponse,
    InboxGetResponse,
    InboxGetPaginatedResponse,
    InboxMarkReadResponse,
    InboxArchiveResponse,
    InboxUnarchiveResponse,
    InboxDeleteResponse,
    InboxEventType,
)

from app.core import supabase
from app.utils import calculate_time_ago
from app.utils.redis_cache import cache_service

logger = logging.getLogger(__name__)


class InboxService:
    CACHE_TTL = 300
    
    def __init__(self):
        pass
    
    def get_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxGetResponse:
        cache_key = f"inbox:{inbox_id}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return InboxGetResponse(**cached)
        
        try:
            response = supabase.table('inbox').select('*').eq('id', str(inbox_id)).eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get inbox: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inbox not found"
            )
        
        inbox_data = response.data[0]
        user_time_zone = self._get_user_time_zone(user_id)
        message_time = calculate_time_ago(inbox_data['created_at'], user_time_zone)
        
        result = InboxGetResponse(
            id=inbox_data['id'],
            title=inbox_data['title'],
            message=inbox_data['message'],
            message_time=message_time,
            is_read=inbox_data.get('is_read', False),
            is_archived=inbox_data.get('is_archived', False),
            event_type=inbox_data.get('event_type'),
            reference_id=inbox_data.get('reference_id'),
        )
        
        # Cache the result
        cache_service.set(cache_key, result.model_dump(mode='json'), ttl=self.CACHE_TTL)
        
        return result
    
    def create_inbox(
        self,
        title: str,
        message: str,
        user_id: UUID4,
        org_id: UUID4,
        user_by: UUID4,
        event_type: Optional[InboxEventType] = None,
        reference_id: Optional[UUID4] = None,
    ) -> InboxResponse:
        
        try:
            insert_data = {
                'title': title,
                'message': message,
                'user_id': str(user_id),
                'org_id': str(org_id),
                'user_by': str(user_by),
                'is_read': False,
                'is_archived': False,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
            
            if event_type:
                insert_data['event_type'] = event_type.value
            if reference_id:
                insert_data['reference_id'] = str(reference_id)
            
            logger.info(f"Creating inbox notification for user {user_id}: {title}")
            response = supabase.table('inbox').insert(insert_data).execute()
            logger.info(f"Inbox notification created successfully: {response.data}")
        except AuthApiError as e:
            logger.error(f"Database error creating inbox: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create inbox: {e}"
            )
        except Exception as e:
            logger.error(f"Unexpected error creating inbox: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create inbox: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Inbox insert returned no data. Response: {response}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create inbox"
            )
        
        inbox_data = response.data[0]
        user_time_zone = self._get_user_time_zone(user_id)
        message_time = calculate_time_ago(inbox_data['created_at'], user_time_zone)
        
        self._invalidate_user_inbox_cache(user_id, org_id)
        
        return InboxResponse(
            id=inbox_data['id'],
            title=inbox_data['title'],
            message=inbox_data['message'],
            message_time=message_time,
            is_read=inbox_data.get('is_read', False),
            is_archived=inbox_data.get('is_archived', False),
            event_type=inbox_data.get('event_type'),
            reference_id=inbox_data.get('reference_id'),
        )
    
    def get_all_inbox(
        self, 
        user_id: UUID4,
        org_id: UUID4,
        include_archived: bool = False,
        unread_only: bool = False,
        order_by: Optional[str] = "desc",
        limit: Optional[int] = 50,
        offset: Optional[int] = 0,
    ) -> InboxGetPaginatedResponse:
        # Normalize order_by to ensure valid value
        if order_by not in ["asc", "desc"]:
            order_by = "desc"
        
        cache_key = f"inbox:list:{user_id}:{org_id}:{include_archived}:{unread_only}:{order_by}:{limit}:{offset}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return InboxGetPaginatedResponse(**cached)
        
        query = supabase.table('inbox').select('*', count='exact').eq('user_id', str(user_id)).eq('org_id', str(org_id))
        
        if not include_archived:
            query = query.eq('is_archived', False)
        
        if unread_only:
            query = query.eq('is_read', False)
        
        # Apply ordering by created_at
        if order_by == "asc":
            query = query.order('created_at', desc=False)
        else:
            query = query.order('created_at', desc=True)
        
        query = query.range(offset, offset + limit - 1)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get all inbox: {e}"
            )
        
        user_time_zone = self._get_user_time_zone(user_id)
            
        inboxes = []
        for inbox in response.data:
            message_time = calculate_time_ago(inbox['created_at'], user_time_zone)
            inboxes.append(InboxResponse(
                id=inbox['id'],
                title=inbox['title'],
                message=inbox['message'],
                message_time=message_time,
                is_read=inbox.get('is_read', False),
                is_archived=inbox.get('is_archived', False),
                event_type=inbox.get('event_type'),
                reference_id=inbox.get('reference_id'),
            ))
        
        result = InboxGetPaginatedResponse(
            inbox=inboxes,
            total=response.count if response.count else 0,
            offset=offset,
            limit=limit,
        )
        
        # Cache the result
        cache_service.set(cache_key, result.model_dump(mode='json'), ttl=self.CACHE_TTL)
        
        return result
    
    def get_archived_inbox(
        self, 
        user_id: UUID4,
        org_id: UUID4,
        limit: Optional[int] = 50,
        offset: Optional[int] = 0,
    ) -> InboxGetPaginatedResponse:
        """Get only archived inbox notifications with pagination."""
        cache_key = f"inbox:archived:{user_id}:{org_id}:{limit}:{offset}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return InboxGetPaginatedResponse(**cached)
        
        query = supabase.table('inbox').select('*', count='exact').eq('user_id', str(user_id)).eq('org_id', str(org_id)).eq('is_archived', True)
        
        query = query.order('created_at', desc=True).range(offset, offset + limit - 1)
        
        try:
            response = query.execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get archived inbox: {e}"
            )
        
        user_time_zone = self._get_user_time_zone(user_id)
            
        inboxes = []
        for inbox in response.data:
            message_time = calculate_time_ago(inbox['created_at'], user_time_zone)
            inboxes.append(InboxResponse(
                id=inbox['id'],
                title=inbox['title'],
                message=inbox['message'],
                message_time=message_time,
                is_read=inbox.get('is_read', False),
                is_archived=inbox.get('is_archived', False),
                event_type=inbox.get('event_type'),
                reference_id=inbox.get('reference_id'),
            ))
        
        result = InboxGetPaginatedResponse(
            inbox=inboxes,
            total=response.count if response.count else 0,
            offset=offset,
            limit=limit,
        )
        
        # Cache the result
        cache_service.set(cache_key, result.model_dump(mode='json'), ttl=self.CACHE_TTL)
        
        return result
    
    def mark_read(self, inbox_id: UUID4, user_id: UUID4) -> InboxMarkReadResponse:
        try:
            response = supabase.table('inbox').update({
                'is_read': True,
                'read_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', str(inbox_id)).eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to mark inbox as read: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inbox not found"
            )
        
        self._invalidate_inbox_cache(inbox_id, user_id, response.data[0].get('org_id'))
        
        return InboxMarkReadResponse(success=True, message="Inbox marked as read")
    
    def archive_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxArchiveResponse:
        try:
            response = supabase.table('inbox').update({
                'is_archived': True,
                'archived_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', str(inbox_id)).eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to archive inbox: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inbox not found"
            )
        
        self._invalidate_inbox_cache(inbox_id, user_id, response.data[0].get('org_id'))
        
        return InboxArchiveResponse(success=True, message="Inbox archived successfully")
    
    def unarchive_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxUnarchiveResponse:
        try:
            response = supabase.table('inbox').update({
                'is_archived': False,
                'archived_at': None,
            }).eq('id', str(inbox_id)).eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to unarchive inbox: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inbox not found"
            )
        
        self._invalidate_inbox_cache(inbox_id, user_id, response.data[0].get('org_id'))
        
        return InboxUnarchiveResponse(success=True, message="Inbox restored successfully")
    
    def delete_inbox(self, inbox_id: UUID4, user_id: UUID4) -> InboxDeleteResponse:
        try:
            check_response = supabase.table('inbox').select('org_id').eq('id', str(inbox_id)).eq('user_id', str(user_id)).execute()
            
            if not check_response.data or len(check_response.data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Inbox not found"
                )
            
            org_id = check_response.data[0].get('org_id')
            
            response = supabase.table('inbox').delete().eq('id', str(inbox_id)).eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete inbox: {e}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(    
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Inbox not found"
            )
        
        self._invalidate_inbox_cache(inbox_id, user_id, org_id)
        
        return InboxDeleteResponse(success=True, message="Inbox deleted successfully")
    
    def get_unread_count(self, user_id: UUID4, org_id: UUID4) -> int:
        cache_key = f"inbox:unread:{user_id}:{org_id}"
        
        # Check cache first (shorter TTL for unread count)
        cached = cache_service.get(cache_key)
        if cached is not None:
            return int(cached)
        
        try:
            response = supabase.table('inbox').select('id', count='exact').eq('user_id', str(user_id)).eq('org_id', str(org_id)).eq('is_read', False).eq('is_archived', False).execute()
            count = response.count if response.count else 0
        except AuthApiError as e:
            logger.error(f"Failed to get unread count: {e}")
            return 0
        
        # Cache the result (shorter TTL for unread count - 60 seconds)
        cache_service.set(cache_key, count, ttl=60)
        
        return count
    
    def _get_user_time_zone(self, user_id: UUID4) -> str:
        cache_key = f"user:timezone:{user_id}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return cached
        
        try:
            response = supabase.table('profiles').select('timezone').eq('user_id', str(user_id)).execute()
        except AuthApiError as e:
            logger.error(f"Failed to get user timezone: {e}")
            return 'UTC'
        
        if not response.data or len(response.data) == 0:
            return 'UTC'
        
        timezone_val = response.data[0].get('timezone', 'UTC')
        
        # Cache the result (1 hour TTL - timezone rarely changes)
        cache_service.set(cache_key, timezone_val, ttl=3600)
        
        return timezone_val
    
    def _invalidate_inbox_cache(self, inbox_id: UUID4, user_id: UUID4, org_id: Optional[str] = None):
        try:
            cache_service.delete(f"inbox:{inbox_id}")
            if org_id:
                self._invalidate_user_inbox_cache(user_id, UUID4(org_id))
        except Exception as e:
            logger.warning(f"Redis delete error: {e}")
    
    def _invalidate_user_inbox_cache(self, user_id: UUID4, org_id: UUID4):
        from app.utils.redis_cache import cache_service
        
        try:
            # Use pattern-based invalidation
            cache_service.invalidate_pattern(f"inbox:list:{user_id}:{org_id}:*")
            cache_service.invalidate_pattern(f"inbox:archived:{user_id}:{org_id}:*")
            cache_service.delete(f"inbox:unread:{user_id}:{org_id}")
        except Exception as e:
            logger.warning(f"Redis delete error: {e}")