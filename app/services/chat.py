import json
from fastapi import HTTPException, status
from pydantic import UUID4
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import logging

from app.core import supabase
from app.schemas.chat import (
    ProjectMessageCreate,
    ProjectMessageResponse,
    ProjectMessageUpdate,
    DirectMessageCreate,
    DirectMessageResponse,
    DirectMessageUpdate,
    MessageReadRequest,
    ConversationCreate,
    ConversationResponse,
    SearchResultResponse,
    NotificationSummaryResponse,
    UnreadCountResponse,
    MessageType,
    ProjectConversationResponse,
    ProjectConversationListResponse,
)
from app.utils import apply_pagination
from app.utils.redis_cache import UserCache
from app.utils.inbox_helpers import trigger_direct_message_notification
from app.services.files import FilesService
from app.core.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()


class ChatService:
    def __init__(self):
        self.files_service = FilesService()
    
    def _get_user_info_with_cache(self, user_id: UUID4) -> Dict[str, Any]:
        """
        Get user information with Redis caching and avatar URL from avatar_file_id.
        This is a unified method to replace duplicate code across the service.
        
        Args:
            user_id: The user ID to fetch
            
        Returns:
            Dict with id, display_name, and avatar_url
        """
        user_id_str = str(user_id)
        
        try:
            # Try to get from cache first
            cached_user = UserCache.get_user(user_id_str)
            
            if cached_user:
                # Return cached info with avatar_url
                avatar_url = None
                if cached_user.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(UUID4(cached_user['avatar_file_id']))
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                
                return {
                    'id': cached_user.get('user_id') or cached_user.get('id'),
                    'display_name': cached_user.get('display_name'),
                    'avatar_url': avatar_url
                }
            
            # Cache miss - fetch from database
            user_response = supabase.table('profiles').select('user_id, display_name, avatar_file_id').eq(
                'user_id', user_id_str
            ).execute()
            
            if user_response.data and len(user_response.data) > 0:
                user = user_response.data[0]
                
                # Get avatar URL from avatar_file_id
                avatar_url = None
                if user.get('avatar_file_id'):
                    try:
                        avatar_url = self.files_service.get_file_url(UUID4(user['avatar_file_id']))
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                
                # Prepare user data for caching (include avatar_file_id for future URL generation)
                user_data_for_cache = {
                    'user_id': user['user_id'],
                    'display_name': user.get('display_name'),
                    'avatar_file_id': user.get('avatar_file_id')
                }
                
                # Cache the user data
                UserCache.set_user(user_id_str, user_data_for_cache)
                
                # Return formatted info
                return {
                    'id': user['user_id'],
                    'display_name': user.get('display_name'),
                    'avatar_url': avatar_url
                }
            else:
                # User not found - set default
                return {
                    'id': user_id_str,
                    'display_name': None,
                    'avatar_url': None
                }
                
        except Exception as e:
            logger.error(f"Error getting user info for {user_id_str}: {str(e)}")
            # Fallback to basic info
            return {
                'id': user_id_str,
                'display_name': None,
                'avatar_url': None
            }
    
    def _batch_get_user_info(self, user_ids: List[UUID4]) -> Dict[str, Dict[str, Any]]:
        """
        Batch fetch user information with Redis caching and avatar URLs.
        This method optimizes N+1 queries by fetching all users in a single database call.
        
        Args:
            user_ids: List of user IDs to fetch
            
        Returns:
            Dict mapping user_id (as string) to user info dict with id, display_name, and avatar_url
        """
        if not user_ids:
            return {}
        
        result = {}
        user_ids_str = [str(uid) for uid in user_ids]
        user_ids_to_fetch = []
        
        # First, try to get from cache
        for user_id_str in user_ids_str:
            try:
                cached_user = UserCache.get_user(user_id_str)
                if cached_user:
                    avatar_url = None
                    if cached_user.get('avatar_file_id'):
                        try:
                            avatar_url = self.files_service.get_file_url(UUID4(cached_user['avatar_file_id']))
                        except Exception as e:
                            logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                    
                    result[user_id_str] = {
                        'id': cached_user.get('user_id') or cached_user.get('id'),
                        'display_name': cached_user.get('display_name'),
                        'avatar_url': avatar_url
                    }
                else:
                    user_ids_to_fetch.append(user_id_str)
            except Exception as e:
                logger.warning(f"Error getting user {user_id_str} from cache: {e}")
                user_ids_to_fetch.append(user_id_str)
        
        # Batch fetch missing users from database
        if user_ids_to_fetch:
            try:
                user_response = supabase.table('profiles').select(
                    'user_id, display_name, avatar_file_id'
                ).in_('user_id', user_ids_to_fetch).execute()
                
                if user_response.data:
                    for user in user_response.data:
                        user_id_str = user['user_id']
                        
                        avatar_url = None
                        if user.get('avatar_file_id'):
                            try:
                                avatar_url = self.files_service.get_file_url(UUID4(user['avatar_file_id']))
                            except Exception as e:
                                logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                        
                        user_data_for_cache = {
                            'user_id': user['user_id'],
                            'display_name': user.get('display_name'),
                            'avatar_file_id': user.get('avatar_file_id')
                        }
                        
                        UserCache.set_user(user_id_str, user_data_for_cache)
                        
                        result[user_id_str] = {
                            'id': user['user_id'],
                            'display_name': user.get('display_name'),
                            'avatar_url': avatar_url
                        }
                
                # Set default for users not found in database
                for user_id_str in user_ids_to_fetch:
                    if user_id_str not in result:
                        result[user_id_str] = {
                            'id': user_id_str,
                            'display_name': None,
                            'avatar_url': None
                        }
                        
            except Exception as e:
                logger.error(f"Error batch fetching user info: {str(e)}")
                # Set default for all failed fetches
                for user_id_str in user_ids_to_fetch:
                    if user_id_str not in result:
                        result[user_id_str] = {
                            'id': user_id_str,
                            'display_name': None,
                            'avatar_url': None
                        }
        
        return result
    
    def send_project_message(
        self,
        project_id: UUID4,
        user_id: UUID4,
        message_data: ProjectMessageCreate
    ) -> ProjectMessageResponse:
        """
        Send a message to a project chat
        
        Args:
            project_id: The project ID
            user_id: The user sending the message
            message_data: The message data
            
        Returns:
            ProjectMessageResponse: The created message
        """
        try:
            message_type = MessageType.FILE if message_data.attachments else MessageType.TEXT
            
            insert_data = {
                'project_id': str(project_id),
                'user_id': str(user_id),
                'body': message_data.body,
                'message_type': message_type.value,
                'reply_to_id': str(message_data.reply_to_id) if message_data.reply_to_id else None,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
            
            if message_data.attachments:
                insert_data['attachments'] = json.dumps([str(att_id) for att_id in message_data.attachments])
            
            response = supabase.table('chat_messages').insert(insert_data).execute()
            
            if not response.data:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create message"
                )
            
            message = response.data[0]
            message_id = message['id']
            
            # Link attachments to the message if any
            if message_data.attachments:
                self._link_attachments_to_message(message_data.attachments, message_id, 'project')
            
            # Normalize read_by field
            message['read_by'] = self._normalize_read_by(message.get('read_by', []))
            
            self._enrich_message_with_user_info(message, user_id)
            
            return ProjectMessageResponse(**message)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error sending project message: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to send message: {str(e)}"
            )
    
    def get_project_messages(
        self,
        project_id: UUID4,
        limit: Optional[int] = 50,
        offset: Optional[int] = 0,
        before_date: Optional[datetime] = None,
        after_date: Optional[datetime] = None,
        search: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get paginated messages for a project
        
        Args:
            project_id: The project ID
            limit: Number of messages to return
            offset: Number of messages to skip
            before_date: Get messages before this date
            after_date: Get messages after this date
            search: Search term for full-text search
            
        Returns:
            Dict containing messages and pagination info
        """
        try:
            query = supabase.table('chat_messages').select(
                'id, body, user_id, project_id, created_at, read_by, message_type, deleted_at, attachments, reply_to_id, edited_at',
                count='exact'
            ).eq('project_id', str(project_id)).is_('deleted_at', 'null')
            
            if search:
                query = query.textSearch('search_vector', f"'{search}'")
            
            if before_date:
                query = query.lt('created_at', before_date.isoformat())
            
            if after_date:
                query = query.gt('created_at', after_date.isoformat())
            
            query = query.order('created_at', desc=True)
            
            limit, offset, query = apply_pagination(query, limit, offset)
            
            response = query.execute()
            
            messages = response.data if response.data else []
            total = response.count if hasattr(response, 'count') else len(messages)
            
            # Collect all unique user IDs for batch fetching
            user_ids = set()
            for message in messages:
                # Normalize read_by field - handle JSONB from database
                raw_read_by = message.get('read_by')
                if raw_read_by is not None:
                    message['read_by'] = self._normalize_read_by(raw_read_by)
                else:
                    message['read_by'] = []
                
                # Collect user_id for batch fetching
                if message.get('user_id'):
                    try:
                        user_ids.add(UUID4(message['user_id']))
                    except Exception:
                        pass
            
            # Batch fetch all user info
            user_info_cache = {}
            if user_ids:
                user_info_cache = self._batch_get_user_info(list(user_ids))
            
            # Enrich messages with batch-fetched user info
            for message in messages:
                user_id = message.get('user_id')
                if user_id:
                    user_id_str = str(user_id)
                    message['user'] = user_info_cache.get(user_id_str) or {
                        'id': user_id_str,
                        'display_name': None,
                        'avatar_url': None
                    }
                else:
                    message['user'] = None
            
            # Ensure all messages have normalized read_by before creating Pydantic models
            for msg in messages:
                if 'read_by' in msg:
                    msg['read_by'] = self._normalize_read_by(msg['read_by'])
                else:
                    msg['read_by'] = []
            
            return {
                'messages': [ProjectMessageResponse(**msg) for msg in messages],
                'total': total,
                'limit': limit,
                'offset': offset
            }
            
        except Exception as e:
            logger.error(f"Error getting project messages: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get messages: {str(e)}"
            )
    
    def edit_message(
        self,
        message_id: UUID4,
        user_id: UUID4,
        message_data: ProjectMessageUpdate,
        is_project_message: bool = True
    ) -> Dict[str, Any]:
        """
        Edit a message (within 24 hours)
        
        Args:
            message_id: The message ID
            user_id: The user editing the message
            message_data: The updated message data
            is_project_message: Whether it's a project message or DM
            
        Returns:
            The updated message
        """
        try:
            table_name = 'chat_messages' if is_project_message else 'direct_messages'
            
            # Select only needed fields for permission check
            fields = 'id, user_id, created_at' if is_project_message else 'id, sender_id, created_at'
            message_response = supabase.table(table_name).select(fields).eq('id', str(message_id)).execute()
            
            if not message_response.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Message not found"
                )
            
            message = message_response.data[0]
            
            user_field = 'user_id' if is_project_message else 'sender_id'
            if message[user_field] != str(user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only edit your own messages"
                )
            
            # Parse created_at and ensure it's timezone-aware
            created_at_str = message['created_at']
            if created_at_str.endswith('Z'):
                created_at_str = created_at_str.replace('Z', '+00:00')
            elif '+' not in created_at_str and created_at_str.count(':') >= 2:
                # If no timezone info, assume UTC
                created_at_str = created_at_str + '+00:00'
            
            created_at = datetime.fromisoformat(created_at_str)
            # Ensure created_at is timezone-aware
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            
            if datetime.now(timezone.utc) - created_at > timedelta(hours=24):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Messages can only be edited within 24 hours"
                )
            
            update_data = {
                'body': message_data.body,
                'edited_at': datetime.now(timezone.utc).isoformat()
            }
            
            response = supabase.table(table_name).update(update_data).eq('id', str(message_id)).execute()
            
            if not response.data:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to update message"
                )
            
            updated_message = response.data[0]
            
            # Normalize read_by field if it's a project message
            if is_project_message:
                updated_message['read_by'] = self._normalize_read_by(updated_message.get('read_by', []))
            
            # Get user_id from the message for enrichment
            user_id_for_enrich = UUID4(updated_message[user_field]) if updated_message.get(user_field) else user_id
            self._enrich_message_with_user_info(updated_message, user_id_for_enrich)
            
            return updated_message
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error editing message: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to edit message: {str(e)}"
            )
    
    def delete_message(
        self,
        message_id: UUID4,
        user_id: UUID4,
        is_project_admin: bool = False,
        is_project_message: bool = True
    ) -> None:
        """
        Soft delete a message
        
        Args:
            message_id: The message ID
            user_id: The user deleting the message
            is_project_admin: Whether the user is a project admin
            is_project_message: Whether it's a project message or DM
        """
        try:
            table_name = 'chat_messages' if is_project_message else 'direct_messages'
            
            # Select only needed fields for permission check
            fields = 'id, user_id' if is_project_message else 'id, sender_id'
            message_response = supabase.table(table_name).select(fields).eq('id', str(message_id)).execute()
            
            if not message_response.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Message not found"
                )
            
            message = message_response.data[0]
            
            user_field = 'user_id' if is_project_message else 'sender_id'
            if message[user_field] != str(user_id) and not is_project_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only delete your own messages"
                )
            
            update_data = {
                'deleted_at': datetime.now(timezone.utc).isoformat()
            }
            
            supabase.table(table_name).update(update_data).eq('id', str(message_id)).execute()
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting message: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete message: {str(e)}"
            )
    
    def mark_project_messages_read(
        self,
        project_id: UUID4,
        user_id: UUID4,
        last_read_message_id: UUID4
    ) -> List[str]:
        """
        Mark project messages as read using a single database request
        
        Args:
            project_id: The project ID
            user_id: The user marking messages as read
            last_read_message_id: ID of the last message read
            
        Returns:
            List of message IDs that were marked as read
        """
        try:
            # Get the created_at of the last read message
            last_message = supabase.table('chat_messages').select('created_at').eq('id', str(last_read_message_id)).execute()
            if not last_message.data:
                return []
            
            last_message_created_at = last_message.data[0]['created_at']
            user_id_str = str(user_id)
            
            # Get all message IDs that need to be updated (for return value)
            message_response = supabase.table('chat_messages').select(
                'id'
            ).eq('project_id', str(project_id)).lte(
                'created_at', last_message_created_at
            ).is_('deleted_at', 'null').execute()
            
            if not message_response.data:
                return []
            
            message_ids = [msg['id'] for msg in message_response.data]
            
            if not message_ids:
                return []
            
            # Use a single UPDATE query with JSONB operations to update all messages at once
            # This uses PostgreSQL's JSONB functions to append user_id to read_by array
            # only if it's not already present
            try:
                # Use RPC function to execute a single SQL update with JSONB operations
                supabase.rpc('mark_project_messages_read_batch', {
                    'p_project_id': str(project_id),
                    'p_user_id': user_id_str,
                    'p_last_message_created_at': last_message_created_at
                }).execute()
                
                return message_ids
                
            except Exception as rpc_error:
                # RPC function is required for efficient batch updates
                # Since Supabase Python client doesn't support raw SQL expressions in updates,
                # we need to use a PostgreSQL function via RPC to perform a single UPDATE
                # with JSONB array operations
                # 
                # The SQL function is provided in 'backend/mark_messages_read_batch_function.sql'
                logger.error(f"RPC function 'mark_project_messages_read_batch' not found: {rpc_error}")
                logger.error("Please execute the SQL in 'backend/mark_messages_read_batch_function.sql' to create the required function.")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Batch update requires RPC function. Please create 'mark_project_messages_read_batch' function in the database. See 'backend/mark_messages_read_batch_function.sql' for the SQL."
                )
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error marking messages as read: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to mark messages as read: {str(e)}"
            )
    
    def send_typing_indicator(
        self,
        reference_id: UUID4,
        user_id: UUID4,
        is_typing: bool,
        chat_type: str = 'project'
    ) -> None:
        """
        Send typing indicator
        
        Args:
            reference_id: The project_id or conversation_id
            user_id: The user typing
            is_typing: Whether user is typing
            chat_type: 'project' or 'direct'
        """
        try:
            if is_typing:
                insert_data = {
                    'chat_type': chat_type,
                    'reference_id': str(reference_id),
                    'user_id': str(user_id),
                    'started_at': datetime.now(timezone.utc).isoformat(),
                    'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
                }
                
                supabase.table('chat_typing_indicators').upsert(
                    insert_data,
                    on_conflict='chat_type,reference_id,user_id'
                ).execute()
            else:
                supabase.table('chat_typing_indicators').delete().eq(
                    'reference_id', str(reference_id)
                ).eq('user_id', str(user_id)).eq('chat_type', chat_type).execute()
            
        except Exception as e:
            logger.error(f"Error sending typing indicator: {str(e)}")
    
    def create_dm_conversation(
        self,
        sender_id: UUID4,
        receiver_id: UUID4,
        organization_id: UUID4
    ) -> ConversationResponse:
        """
        Create or get a DM conversation
        
        Args:
            sender_id: The user creating the conversation
            receiver_id: The user receiving the conversation
            organization_id: The organization ID
            
        Returns:
            ConversationResponse: The conversation
        """
        try:
            if sender_id == receiver_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot create conversation with yourself"
                )
            
            self._verify_same_organization(
                sender_id,
                receiver_id,
                organization_id
            )
            
            user1_id = min(str(sender_id), str(receiver_id))
            user2_id = max(str(sender_id), str(receiver_id))
            
            existing_conv = supabase.table('chat_conversations').select(
                'id, user1_id, user2_id, organization_id, last_message_at, created_at'
            ).eq('user1_id', user1_id).eq('user2_id', user2_id).eq(
                'organization_id', str(organization_id)
            ).execute()
            
            if existing_conv.data:
                conversation = existing_conv.data[0]
            else:
                insert_data = {
                    'user1_id': user1_id,
                    'user2_id': user2_id,
                    'organization_id': str(organization_id),
                    'last_message_at': datetime.now(timezone.utc).isoformat(),
                    'created_at': datetime.now(timezone.utc).isoformat()
                }
                
                response = supabase.table('chat_conversations').insert(insert_data).execute()
                
                if not response.data:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to create conversation"
                    )
                
                conversation = response.data[0]
            
            self._enrich_conversation_with_user_info(conversation, sender_id)
            
            return ConversationResponse(**conversation)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating conversation: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create conversation: {str(e)}"
            )
    
    def get_dm_conversations(
        self,
        user_id: UUID4,
        organization_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get all DM conversations for a user
        
        Args:
            user_id: The user ID
            organization_id: The organization ID
            limit: Number of conversations to return
            offset: Number of conversations to skip
            
        Returns:
            Dict containing conversations and pagination info
        """
        try:
            query = supabase.table('chat_conversations').select(
                'id, user1_id, user2_id, organization_id, last_message_at, created_at',
                count='exact'
            ).eq('organization_id', str(organization_id)).or_(
                f"user1_id.eq.{user_id},user2_id.eq.{user_id}"
            ).order('last_message_at', desc=True)
            
            limit, offset, query = apply_pagination(query, limit, offset)
            
            response = query.execute()
            
            conversations = response.data if response.data else []
            total = response.count if hasattr(response, 'count') else len(conversations)
            
            # Collect all unique other_user_ids and conversation IDs for batch fetching
            user_ids = set()
            conversation_ids = []
            for conversation in conversations:
                other_user_id = conversation.get('user2_id') if conversation.get('user1_id') == str(user_id) else conversation.get('user1_id')
                if other_user_id:
                    try:
                        user_ids.add(UUID4(other_user_id))
                    except Exception:
                        pass
                conversation_ids.append(conversation['id'])
            
            # Batch fetch all user info
            user_info_cache = {}
            if user_ids:
                user_info_cache = self._batch_get_user_info(list(user_ids))
            
            # Batch fetch all unread counts for conversations
            unread_counts = {}
            if conversation_ids:
                conversation_ids_str = [str(cid) for cid in conversation_ids]
                unread_response = supabase.table('chat_notifications').select(
                    'reference_id, unread_count'
                ).eq('user_id', str(user_id)).eq('chat_type', 'direct').in_(
                    'reference_id', conversation_ids_str
                ).execute()
                
                if unread_response.data:
                    unread_counts = {
                        item['reference_id']: item.get('unread_count', 0) or 0
                        for item in unread_response.data
                    }
            
            # Enrich conversations with batch-fetched user info and unread counts
            for conversation in conversations:
                other_user_id = conversation.get('user2_id') if conversation.get('user1_id') == str(user_id) else conversation.get('user1_id')
                
                if other_user_id:
                    other_user_id_str = str(other_user_id)
                    conversation['other_user'] = user_info_cache.get(other_user_id_str) or {
                        'id': other_user_id_str,
                        'display_name': None,
                        'avatar_url': None
                    }
                else:
                    conversation['other_user'] = None
                
                # Set unread count from batch-fetched data
                conversation_id_str = str(conversation['id'])
                conversation['unread_count'] = unread_counts.get(conversation_id_str, 0)
            
            return {
                'conversations': [ConversationResponse(**conv) for conv in conversations],
                'total': total,
                'limit': limit,
                'offset': offset
            }
            
        except Exception as e:
            logger.error(f"Error getting conversations: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get conversations: {str(e)}"
            )
    
    def get_project_conversations(
        self,
        user_id: UUID4,
        organization_id: UUID4,
        limit: Optional[int] = None,
        offset: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get all project conversations for a user
        
        Args:
            user_id: The user ID
            organization_id: The organization ID
            limit: Number of conversations to return
            offset: Number of conversations to skip
            
        Returns:
            Dict containing project conversations and pagination info
        """
        try:
            # Get all projects where user is a member
            projects_response = supabase.rpc('get_member_projects', {
                'user_id': str(user_id),
                'org_id': str(organization_id),
            }).eq('archived', False).execute()
            
            if not projects_response.data:
                return {
                    'conversations': [],
                    'total': 0,
                    'limit': limit,
                    'offset': offset
                }
            
            project_ids = [p['id'] for p in projects_response.data]
            
            # Batch fetch all unread counts for projects
            unread_counts = {}
            if project_ids:
                project_ids_str = [str(pid) for pid in project_ids]
                unread_response = supabase.table('chat_notifications').select(
                    'reference_id, unread_count'
                ).eq('user_id', str(user_id)).eq('chat_type', 'project').in_(
                    'reference_id', project_ids_str
                ).execute()
                
                if unread_response.data:
                    unread_counts = {
                        item['reference_id']: item.get('unread_count', 0) or 0
                        for item in unread_response.data
                    }
            
            # Get last message for each project
            conversations_data = []
            for project in projects_response.data:
                project_id = project['id']
                project_id_str = str(project_id)
                
                # Get last message
                last_message_response = supabase.table('chat_messages').select(
                    'id, body, created_at'
                ).eq('project_id', project_id).is_('deleted_at', 'null').order(
                    'created_at', desc=True
                ).limit(1).execute()
                
                last_message = None
                last_message_at = None
                last_message_preview = None
                
                if last_message_response.data and len(last_message_response.data) > 0:
                    last_message = last_message_response.data[0]
                    # Parse datetime - handle both with and without timezone
                    created_at_str = last_message['created_at']
                    if created_at_str.endswith('Z'):
                        created_at_str = created_at_str.replace('Z', '+00:00')
                    last_message_at = datetime.fromisoformat(created_at_str)
                    # Ensure timezone-aware (if naive, assume UTC)
                    if last_message_at.tzinfo is None:
                        last_message_at = last_message_at.replace(tzinfo=timezone.utc)
                    # Get preview (first 100 chars)
                    body = last_message.get('body', '')
                    if body:
                        last_message_preview = body[:100] + ('...' if len(body) > 100 else '')
                    else:
                        last_message_preview = '[File attachment]'
                
                # Get unread count from batch-fetched data
                unread_count = unread_counts.get(project_id_str, 0)
                
                # Get project avatar URL if available
                avatar_url = None
                if project.get('avatar_file_id'):
                    avatar_url = self.files_service.get_file_url(project['avatar_file_id'])
                
                conversations_data.append({
                    'project_id': project_id,
                    'project_name': project['name'],
                    'avatar_color': project.get('avatar_color'),
                    'avatar_icon': project.get('avatar_icon'),
                    'avatar_url': avatar_url,
                    'last_message_at': last_message_at,
                    'last_message_preview': last_message_preview,
                    'unread_count': unread_count
                })
            
            # Sort by last_message_at (newest first), projects with no messages go to end
            # Ensure all datetimes are timezone-aware for comparison
            def get_sort_key(x):
                if x['last_message_at'] is None:
                    return datetime.min.replace(tzinfo=timezone.utc)
                dt = x['last_message_at']
                # Ensure timezone-aware
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            
            conversations_data.sort(key=get_sort_key, reverse=True)
            
            # Apply pagination
            total = len(conversations_data)
            if offset is not None:
                conversations_data = conversations_data[offset:]
            if limit is not None:
                conversations_data = conversations_data[:limit]
            
            return {
                'conversations': [ProjectConversationResponse(**conv) for conv in conversations_data],
                'total': total,
                'limit': limit,
                'offset': offset
            }
            
        except Exception as e:
            logger.error(f"Error getting project conversations: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get project conversations: {str(e)}"
            )
    
    def send_direct_message(
        self,
        conversation_id: UUID4,
        sender_id: UUID4,
        message_data: DirectMessageCreate
    ) -> DirectMessageResponse:
        """
        Send a direct message
        
        Args:
            conversation_id: The conversation ID
            sender_id: The user sending the message
            message_data: The message data
            
        Returns:
            DirectMessageResponse: The created message
        """
        try:
            conversation = self._get_conversation(conversation_id, sender_id)
            
            receiver_id = conversation['user2_id'] if conversation['user1_id'] == str(sender_id) else conversation['user1_id']
            
            message_type = MessageType.FILE if message_data.attachments else MessageType.TEXT
            
            insert_data = {
                'sender_id': str(sender_id),
                'receiver_id': receiver_id,
                'organization_id': conversation['organization_id'],
                'body': message_data.body,
                'message_type': message_type.value,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
            
            if message_data.attachments:
                insert_data['attachments'] = json.dumps([str(att_id) for att_id in message_data.attachments])
            
            response = supabase.table('direct_messages').insert(insert_data).execute()
            
            if not response.data:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create message"
                )
            
            message = response.data[0]
            message_id = message['id']
            
            # Link attachments to the message if any
            if message_data.attachments:
                self._link_attachments_to_message(message_data.attachments, message_id, 'direct')
            
            self._enrich_dm_with_user_info(message, sender_id, str(receiver_id) if receiver_id else None)
            
            try:
                sender_profile = self.files_service._get_user_profile(sender_id)
                sender_name = sender_profile.display_name or 'Someone'
                message_preview = message_data.body[:100] if message_data.body else "Sent an attachment"
                
                trigger_direct_message_notification(
                    user_id=UUID4(receiver_id),
                    org_id=UUID4(conversation['organization_id']),
                    sender_id=sender_id,
                    sender_name=sender_name,
                    message_preview=message_preview,
                    conversation_id=conversation_id,
                )
            except Exception as e:
                logger.error(f"Failed to send DM inbox notification: {e}")
            
            return DirectMessageResponse(**message)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error sending direct message: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to send message: {str(e)}"
            )
    
    def get_direct_messages(
        self,
        conversation_id: UUID4,
        user_id: UUID4,
        limit: Optional[int] = 50,
        offset: Optional[int] = 0,
        before_date: Optional[datetime] = None,
        after_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Get messages for a DM conversation
        
        Args:
            conversation_id: The conversation ID
            user_id: The requesting user ID
            limit: Number of messages to return
            offset: Number of messages to skip
            before_date: Get messages before this date
            after_date: Get messages after this date
            
        Returns:
            Dict containing messages and pagination info
        """
        try:
            conversation = self._get_conversation(conversation_id, user_id)
            
            query = supabase.table('direct_messages').select(
                'id, body, sender_id, receiver_id, created_at, deleted_at, message_type, attachments, read_at, organization_id',
                count='exact'
            ).or_(
                f"sender_id.eq.{conversation['user1_id']},sender_id.eq.{conversation['user2_id']}"
            ).or_(
                f"receiver_id.eq.{conversation['user1_id']},receiver_id.eq.{conversation['user2_id']}"
            ).is_('deleted_at', 'null')
            
            if before_date:
                query = query.lt('created_at', before_date.isoformat())
            
            if after_date:
                query = query.gt('created_at', after_date.isoformat())
            
            query = query.order('created_at', desc=True)
            
            limit, offset, query = apply_pagination(query, limit, offset)
            
            response = query.execute()
            
            messages = response.data if response.data else []
            total = response.count if hasattr(response, 'count') else len(messages)
            
            # Collect all unique sender and receiver IDs for batch fetching
            user_ids = set()
            for message in messages:
                if message.get('sender_id'):
                    try:
                        user_ids.add(UUID4(message['sender_id']))
                    except Exception:
                        pass
                if message.get('receiver_id'):
                    try:
                        user_ids.add(UUID4(message['receiver_id']))
                    except Exception:
                        pass
            
            # Batch fetch all user info
            user_info_cache = {}
            if user_ids:
                user_info_cache = self._batch_get_user_info(list(user_ids))
            
            # Enrich messages with batch-fetched user info
            for message in messages:
                sender_id = message.get('sender_id')
                receiver_id = message.get('receiver_id')
                
                if sender_id:
                    sender_id_str = str(sender_id)
                    message['sender'] = user_info_cache.get(sender_id_str) or {
                        'id': sender_id_str,
                        'display_name': None,
                        'avatar_url': None
                    }
                else:
                    message['sender'] = None
                
                if receiver_id:
                    receiver_id_str = str(receiver_id)
                    message['receiver'] = user_info_cache.get(receiver_id_str) or {
                        'id': receiver_id_str,
                        'display_name': None,
                        'avatar_url': None
                    }
                else:
                    message['receiver'] = None
            
            return {
                'messages': [DirectMessageResponse(**msg) for msg in messages],
                'total': total,
                'limit': limit,
                'offset': offset
            }
            
        except Exception as e:
            logger.error(f"Error getting direct messages: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get messages: {str(e)}"
            )
    
    def mark_dm_read(
        self,
        conversation_id: UUID4,
        user_id: UUID4,
        last_read_message_id: UUID4
    ) -> List[str]:
        """
        Mark direct messages as read
        
        Args:
            conversation_id: The conversation ID
            user_id: The user marking messages as read
            last_read_message_id: ID of the last message read
            
        Returns:
            List of message IDs that were marked as read
        """
        try:
            conversation = self._get_conversation(conversation_id, user_id)
            
            last_message = supabase.table('direct_messages').select(
                'created_at'
            ).eq('id', str(last_read_message_id)).execute()
            
            if not last_message.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Message not found"
                )
            
            # Get the other participant in the conversation
            other_user_id = conversation['user2_id'] if conversation['user1_id'] == str(user_id) else conversation['user1_id']
            
            # Get message IDs that will be updated (for broadcasting) - do this before update
            # Filter by conversation participants to ensure we only update messages in this conversation
            unread_messages = supabase.table('direct_messages').select('id').eq(
                'receiver_id', str(user_id)
            ).eq('sender_id', other_user_id).lte(
                'created_at', last_message.data[0]['created_at']
            ).is_('read_at', 'null').execute()
            
            message_ids = [msg['id'] for msg in (unread_messages.data or [])]
            
            # Batch update all messages in a single query
            # Filter by conversation participants to ensure we only update messages in this conversation
            if message_ids:
                read_at = datetime.now(timezone.utc).isoformat()
                supabase.table('direct_messages').update({
                    'read_at': read_at
                }).eq('receiver_id', str(user_id)).eq(
                    'sender_id', other_user_id
                ).lte(
                    'created_at', last_message.data[0]['created_at']
                ).is_('read_at', 'null').execute()
            
            return message_ids
            
        except Exception as e:
            logger.error(f"Error marking DM as read: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to mark messages as read: {str(e)}"
            )
    
    def search_messages(
        self,
        user_id: UUID4,
        organization_id: UUID4,
        search_term: str,
        chat_type: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        Search across all accessible messages
        
        Args:
            user_id: The user performing the search
            organization_id: The organization ID
            search_term: The search term
            chat_type: Optional filter by 'project' or 'direct'
            limit: Number of results to return
            offset: Number of results to skip
            
        Returns:
            Dict containing search results and pagination info
        """
        try:
            results = []
            
            if not chat_type or chat_type == 'project':
                project_results = self._search_project_messages(user_id, search_term, limit, offset)
                results.extend(project_results)
            
            if not chat_type or chat_type == 'direct':
                dm_results = self._search_direct_messages(user_id, organization_id, search_term, limit, offset)
                results.extend(dm_results)
            
            results.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
            
            return {
                'results': results[:limit],
                'total': len(results),
                'limit': limit,
                'offset': offset
            }
            
        except Exception as e:
            logger.error(f"Error searching messages: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to search messages: {str(e)}"
            )
    
    def get_unread_summary(
        self,
        user_id: UUID4,
        organization_id: UUID4
    ) -> NotificationSummaryResponse:
        """
        Get unread message summary
        
        Args:
            user_id: The user ID
            organization_id: The organization ID
            
        Returns:
            NotificationSummaryResponse: The unread summary
        """
        try:
            notifications_response = supabase.table('chat_notifications').select(
                'unread_count, chat_type, reference_id, updated_at'
            ).eq('user_id', str(user_id)).gt('unread_count', 0).execute()
            
            notifications = notifications_response.data if notifications_response.data else []
            
            project_chats = []
            direct_messages = []
            total_unread = 0
            
            for notif in notifications:
                total_unread += notif['unread_count']
                
                unread_count = UnreadCountResponse(
                    chat_type=notif['chat_type'],
                    reference_id=notif['reference_id'],
                    reference_name=self._get_reference_name(notif['reference_id'], notif['chat_type']),
                    unread_count=notif['unread_count'],
                    last_message_preview=None,
                    last_message_at=notif.get('updated_at')
                )
                
                if notif['chat_type'] == 'project':
                    project_chats.append(unread_count)
                else:
                    direct_messages.append(unread_count)
            
            return NotificationSummaryResponse(
                total_unread=total_unread,
                project_chats=project_chats,
                direct_messages=direct_messages
            )
            
        except Exception as e:
            logger.error(f"Error getting unread summary: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get unread summary: {str(e)}"
            )
    
    def _link_attachments_to_message(self, attachment_ids: List[UUID4], message_id: str, message_type: str) -> None:
        """
        Link uploaded attachments to a message after message creation
        
        Args:
            attachment_ids: List of attachment IDs to link
            message_id: The message ID to link attachments to (as string)
            message_type: 'project' or 'direct'
        """
        try:
            if not attachment_ids:
                return
            
            # Update all attachments to link them to the message
            # Only update attachments that are currently unlinked (message_id is null)
            attachment_id_strings = [str(att_id) for att_id in attachment_ids]
            
            supabase.table('chat_attachments').update({
                'message_id': message_id
            }).in_('id', attachment_id_strings).eq('message_type', message_type).is_('message_id', 'null').execute()
            
            logger.info(f"Linked {len(attachment_ids)} attachments to {message_type} message {message_id}")
            
        except Exception as e:
            logger.warning(f"Failed to link attachments to message {message_id}: {str(e)}")
            # Don't raise exception - message is already created, attachment linking is secondary
    
    def _normalize_read_by(self, read_by: Any) -> List[str]:
        """
        Normalize read_by field from database (JSONB) to list of UUID strings
        
        Args:
            read_by: The read_by value from database (can be JSON string, list, or None)
            
        Returns:
            List of UUID strings
        """
        # Handle None
        if read_by is None:
            return []
        
        # Handle string - Supabase JSONB fields can come as JSON strings
        if isinstance(read_by, str):
            read_by_stripped = read_by.strip()
            
            # Empty string or empty array string
            if not read_by_stripped or read_by_stripped == '[]':
                return []
            
            # Try to parse as JSON
            try:
                parsed = json.loads(read_by_stripped)
                # Recursively process if we got a list
                if isinstance(parsed, list):
                    read_by = parsed
                elif isinstance(parsed, str):
                    # If it's a string representation of JSON, parse again
                    try:
                        parsed = json.loads(parsed)
                        if isinstance(parsed, list):
                            read_by = parsed
                        else:
                            logger.warning(f"Double-parsed read_by is not a list: {type(parsed)}, value: {parsed}")
                            return []
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"Failed to double-parse read_by: {read_by}")
                        return []
                else:
                    logger.warning(f"Parsed read_by is not a list: {type(parsed)}, value: {parsed}")
                    return []
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse read_by as JSON: {read_by}, error: {e}")
                return []
        
        # Handle list - ensure all items are converted to strings
        if isinstance(read_by, list):
            result = []
            for item in read_by:
                if item is None:
                    continue
                
                # Convert to string
                item_str = str(item).strip()
                
                # If item is itself a JSON string (like '["uuid"]'), parse it
                if item_str.startswith('[') and item_str.endswith(']'):
                    try:
                        item_parsed = json.loads(item_str)
                        if isinstance(item_parsed, list):
                            result.extend([str(x).strip() for x in item_parsed if x])
                        else:
                            result.append(str(item_parsed).strip())
                    except (json.JSONDecodeError, TypeError):
                        # If parsing fails, just use the string as-is
                        result.append(item_str)
                elif item_str:
                    result.append(item_str)
            
            return result
        
        # If it's some other type, return empty list
        logger.warning(f"Unexpected read_by type: {type(read_by)}, value: {read_by}")
        return []
    
    def _enrich_message_with_user_info(self, message: Dict[str, Any], user_id: Optional[UUID4] = None) -> None:
        """Add user information to message with Redis caching"""
        # Get user_id from parameter or message
        if not user_id:
            user_id = message.get('user_id')
        
        if not user_id:
            message['user'] = None
            return
        
        # Convert to UUID4 if it's a string
        if isinstance(user_id, str):
            user_id = UUID4(user_id)
        
        message['user'] = self._get_user_info_with_cache(user_id)
    
    def _enrich_dm_with_user_info(self, message: Dict[str, Any], sender_id: Optional[UUID4] = None, receiver_id: Optional[str] = None) -> None:
        """Add user information to direct message with Redis caching"""
        # Get sender_id and receiver_id from parameters or message
        if not sender_id:
            sender_id = message.get('sender_id')
        if not receiver_id:
            receiver_id = message.get('receiver_id')
        
        # Get sender info
        if sender_id:
            message['sender'] = self._get_user_info_with_cache(UUID4(sender_id) if isinstance(sender_id, str) else sender_id)
        else:
            message['sender'] = None
        
        # Get receiver info
        if receiver_id:
            message['receiver'] = self._get_user_info_with_cache(UUID4(receiver_id) if isinstance(receiver_id, str) else UUID4(receiver_id))
        else:
            message['receiver'] = None
    
    def _enrich_conversation_with_user_info(self, conversation: Dict[str, Any], current_user_id: UUID4) -> None:
        """Add other user information to conversation with Redis caching"""
        try:
            other_user_id = conversation['user2_id'] if conversation['user1_id'] == str(current_user_id) else conversation['user1_id']
            
            if not other_user_id:
                conversation['other_user'] = None
                return
            
            conversation['other_user'] = self._get_user_info_with_cache(UUID4(other_user_id))
            
        except Exception as e:
            logger.error(f"Error enriching conversation with user info: {str(e)}")
            # Fallback
            other_user_id = conversation.get('user2_id') if conversation.get('user1_id') == str(current_user_id) else conversation.get('user1_id')
            conversation['other_user'] = {
                'id': str(other_user_id) if other_user_id else None,
                'display_name': None,
                'avatar_url': None
            } if other_user_id else None
    
    def _add_unread_count_to_conversation(self, conversation: Dict[str, Any], user_id: UUID4) -> None:
        """Add unread count to conversation"""
        try:
            unread_response = supabase.table('chat_notifications').select('unread_count').eq(
                'user_id', str(user_id)
            ).eq('chat_type', 'direct').eq('reference_id', conversation['id']).execute()
            
            conversation['unread_count'] = unread_response.data[0]['unread_count'] if unread_response.data else 0
        except Exception as e:
            logger.warning(f"Could not get unread count: {str(e)}")
            conversation['unread_count'] = 0
    
    def _get_conversation(self, conversation_id: UUID4, user_id: UUID4) -> Dict[str, Any]:
        """Get and verify conversation access"""
        conversation_response = supabase.table('chat_conversations').select(
            'id, user1_id, user2_id, organization_id, last_message_at, created_at'
        ).eq('id', str(conversation_id)).execute()
        
        if not conversation_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found"
            )
        
        conversation = conversation_response.data[0]
        
        if conversation['user1_id'] != str(user_id) and conversation['user2_id'] != str(user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this conversation"
            )
        
        return conversation
    
    def _verify_same_organization(self, user1_id: UUID4, user2_id: UUID4, organization_id: UUID4) -> None:
        """Verify both users belong to the same organization"""
        user1_response = supabase.table('organization_members').select('id').eq(
            'user_id', str(user1_id)
        ).eq('org_id', str(organization_id)).execute()
        
        user2_response = supabase.table('organization_members').select('id').eq(
            'user_id', str(user2_id)
        ).eq('org_id', str(organization_id)).execute()
        
        if not user1_response.data or not user2_response.data:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Both users must belong to the same organization"
            )
    
    def _search_project_messages(self, user_id: UUID4, search_term: str, limit: int, offset: int) -> Dict[str, Any]:
        """Search project messages accessible to user"""
        try:
            query = supabase.table('project_members').select('project_id').eq(
                'user_id', str(user_id)
            )
            
            # apply pagination
            limit, offset, query = apply_pagination(query, limit, offset)
            
            projects_response = query.execute()
            
            if not projects_response.data:
                return []
            
            project_ids = [p['project_id'] for p in projects_response.data]
            
            results = []
            all_messages = []
            messages_count = 0
            
            # Use single query with IN clause across all projects (not limited to 10)
            if project_ids:
                # Convert to strings for IN clause
                project_ids_str = [str(pid) for pid in project_ids]
                messages_response = supabase.table('chat_messages').select(
                    'id, body, user_id, project_id, created_at, read_by, message_type, deleted_at, attachments, reply_to_id, edited_at',
                    count='exact'
                ).in_('project_id', project_ids_str).textSearch(
                    'search_vector', f"'{search_term}'"
                ).is_('deleted_at', 'null').order('created_at', desc=True).limit(limit).execute()
                
                if messages_response.data:
                    all_messages = messages_response.data
                
                # Get count from response if available
                if hasattr(messages_response, 'count'):
                    messages_count = messages_response.count
                else:
                    messages_count = len(all_messages)
            
            # Collect all unique user IDs for batch fetching
            user_ids = set()
            for msg in all_messages:
                if msg.get('user_id'):
                    try:
                        user_ids.add(UUID4(msg['user_id']))
                    except Exception:
                        pass
            
            # Batch fetch all user info
            user_info_cache = {}
            if user_ids:
                user_info_cache = self._batch_get_user_info(list(user_ids))
            
            # Enrich messages and build results
            for msg in all_messages:
                user_id = msg.get('user_id')
                if user_id:
                    user_id_str = str(user_id)
                    msg['user'] = user_info_cache.get(user_id_str) or {
                        'id': user_id_str,
                        'display_name': None,
                        'avatar_url': None
                    }
                else:
                    msg['user'] = None
                
                results.append({
                    'message_id': msg['id'],
                    'chat_type': 'project',
                    'reference_id': msg['project_id'],
                    'body': msg.get('body'),
                    'user_id': msg['user_id'],
                    'user': msg.get('user'),
                    'created_at': msg['created_at'],
                    'relevance_score': 1.0
                })
            
            return {
                'messages': results,
                'total': messages_count,
                'limit': limit,
                'offset': offset
            }
        except Exception as e:
            logger.error(f"Error searching project messages: {str(e)}")
            return []
    
    def _search_direct_messages(self, user_id: UUID4, organization_id: UUID4, search_term: str, limit: int, offset: int) -> List[Dict]:
        """Search direct messages accessible to user"""
        try:
            query = supabase.table('direct_messages').select(
                'id, body, sender_id, receiver_id, created_at, deleted_at, message_type, attachments, read_at, organization_id',
                count='exact'
            ).or_(
                f"sender_id.eq.{user_id},receiver_id.eq.{user_id}"
            ).eq('organization_id', str(organization_id)).textSearch(
                'search_vector', f"'{search_term}'"
            ).is_('deleted_at', 'null')
            
            limit, offset, query = apply_pagination(query, limit, offset)
            
            messages_response = query.execute()            
            
            results = []
            messages = messages_response.data if messages_response.data else []
            
            # Collect all unique sender and receiver IDs for batch fetching
            user_ids = set()
            for msg in messages:
                if msg.get('sender_id'):
                    try:
                        user_ids.add(UUID4(msg['sender_id']))
                    except Exception:
                        pass
                if msg.get('receiver_id'):
                    try:
                        user_ids.add(UUID4(msg['receiver_id']))
                    except Exception:
                        pass
            
            # Batch fetch all user info
            user_info_cache = {}
            if user_ids:
                user_info_cache = self._batch_get_user_info(list(user_ids))
            
            # Enrich messages and build results
            for msg in messages:
                sender_id = msg.get('sender_id')
                receiver_id = msg.get('receiver_id')
                
                if sender_id:
                    sender_id_str = str(sender_id)
                    msg['sender'] = user_info_cache.get(sender_id_str) or {
                        'id': sender_id_str,
                        'display_name': None,
                        'avatar_url': None
                    }
                else:
                    msg['sender'] = None
                
                if receiver_id:
                    receiver_id_str = str(receiver_id)
                    msg['receiver'] = user_info_cache.get(receiver_id_str) or {
                        'id': receiver_id_str,
                        'display_name': None,
                        'avatar_url': None
                    }
                else:
                    msg['receiver'] = None
                
                results.append({
                    'message_id': msg['id'],
                    'chat_type': 'direct',
                    'reference_id': msg['id'],
                    'body': msg.get('body'),
                    'user_id': msg['sender_id'],
                    'user': msg.get('sender'),
                    'created_at': msg['created_at'],
                    'relevance_score': 1.0
                })
            
            return {
                'messages': results,
                'total': messages_response.count,
                'limit': limit,
                'offset': offset
            }
        except Exception as e:
            logger.error(f"Error searching direct messages: {str(e)}")
            return []
    
    def _get_reference_name(self, reference_id: str, chat_type: str) -> str:
        """Get name for reference (project name or user name)"""
        try:
            if chat_type == 'project':
                response = supabase.table('projects').select('name').eq('id', reference_id).execute()
                return response.data[0]['name'] if response.data else 'Unknown Project'
            else:
                response = supabase.table('chat_conversations').select(
                    'user1_id, user2_id'
                ).eq('id', reference_id).execute()
                if response.data:
                    conv = response.data[0]
                    user_id = conv.get('user2_id')
                    if user_id:
                        user_info = self._get_user_info_with_cache(UUID4(user_id))
                        return user_info.get('display_name') or 'Unknown User'
                    return 'Unknown User'
                return 'Unknown User'
        except Exception as e:
            logger.warning(f"Error getting reference name: {str(e)}")
            return 'Unknown'

