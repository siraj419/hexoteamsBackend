import json
from fastapi import HTTPException, status
from pydantic import UUID4
from typing import List, Optional, Dict, Any

from app.utils.uuid_compat import as_uuid
from datetime import datetime, timezone, timedelta
import logging

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.sync_session import SyncSessionLocal
from app.models import (
    Profile,
    Project,
    ProjectMember,
    ChatMessage,
    DirectMessage,
    ChatConversation,
    ChatNotification,
    ChatTypingIndicator,
    ChatAttachment,
    OrganizationMember,
)
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
from app.utils import apply_pagination, apply_sa_limit_offset
from app.utils.redis_cache import UserCache, cache_service
from app.utils.inbox_helpers import (
    trigger_direct_message_notification,
    trigger_project_chat_message_notification,
)
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
                        avatar_url = self.files_service.get_file_url(as_uuid(cached_user['avatar_file_id']))
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                
                return {
                    'id': cached_user.get('user_id') or cached_user.get('id'),
                    'display_name': cached_user.get('display_name'),
                    'avatar_url': avatar_url
                }
            
            # Cache miss - fetch from database
            db = SyncSessionLocal()
            try:
                p = db.execute(select(Profile).where(Profile.user_id == as_uuid(user_id_str))).scalar_one_or_none()
            finally:
                db.close()

            if p:
                user = {
                    "user_id": str(p.user_id),
                    "display_name": p.display_name,
                    "email": p.email,
                    "avatar_file_id": str(p.avatar_file_id) if p.avatar_file_id else None,
                }
                
                # Get avatar URL from avatar_file_id
                avatar_url = None
                if user.get("avatar_file_id"):
                    try:
                        avatar_url = self.files_service.get_file_url(as_uuid(user["avatar_file_id"]))
                    except Exception as e:
                        logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")
                
                # Prepare user data for caching (include email and avatar_file_id)
                # Standardize on 'id' key for consistency across services
                user_data_for_cache = {
                    "id": user["user_id"],
                    "display_name": user.get("display_name"),
                    "email": user.get("email"),
                    "avatar_file_id": user.get("avatar_file_id"),
                }
                
                # Cache the user data
                UserCache.set_user(user_id_str, user_data_for_cache)
                
                # Return formatted info
                return {
                    "id": user["user_id"],
                    "display_name": user.get("display_name"),
                    "avatar_url": avatar_url,
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
                            avatar_url = self.files_service.get_file_url(as_uuid(cached_user['avatar_file_id']))
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
                uuids = [as_uuid(x) for x in user_ids_to_fetch]
                db = SyncSessionLocal()
                try:
                    profiles = db.execute(select(Profile).where(Profile.user_id.in_(uuids))).scalars().all()
                finally:
                    db.close()

                if profiles:
                    for user in profiles:
                        user_id_str = str(user.user_id)
                        
                        avatar_url = None
                        if user.avatar_file_id:
                            try:
                                avatar_url = self.files_service.get_file_url(user.avatar_file_id)
                            except Exception as e:
                                logger.warning(f"Failed to get avatar URL for user {user_id_str}: {e}")

                        user_data_for_cache = {
                            "id": str(user.user_id),
                            "display_name": user.display_name,
                            "email": user.email,
                            "avatar_file_id": str(user.avatar_file_id) if user.avatar_file_id else None,
                        }

                        UserCache.set_user(user_id_str, user_data_for_cache)

                        result[user_id_str] = {
                            "id": str(user.user_id),
                            "display_name": user.display_name,
                            "avatar_url": avatar_url,
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

            att_json = None
            if message_data.attachments:
                att_json = [str(att_id) for att_id in message_data.attachments]

            db = SyncSessionLocal()
            try:
                cm = ChatMessage(
                    project_id=project_id,
                    user_id=user_id,
                    body=message_data.body,
                    message_type=message_type.value,
                    reply_to_id=message_data.reply_to_id,
                    attachments=att_json,
                )
                db.add(cm)
                db.commit()
                db.refresh(cm)
                message = {
                    "id": str(cm.id),
                    "project_id": str(cm.project_id),
                    "user_id": str(cm.user_id),
                    "body": cm.body,
                    "message_type": cm.message_type,
                    "reply_to_id": str(cm.reply_to_id) if cm.reply_to_id else None,
                    "read_by": cm.read_by,
                    "attachments": cm.attachments,
                    "created_at": cm.created_at.isoformat() if cm.created_at else None,
                    "edited_at": cm.edited_at.isoformat() if cm.edited_at else None,
                    "deleted_at": cm.deleted_at.isoformat() if cm.deleted_at else None,
                }
                message_id = message["id"]
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            if not message_id:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create message",
                )
            
            # Link attachments to the message if any
            if message_data.attachments:
                self._link_attachments_to_message(message_data.attachments, message_id, 'project')
            
            # Normalize read_by field
            message['read_by'] = self._normalize_read_by(message.get('read_by', []))
            
            self._enrich_message_with_user_info(message, user_id)

            try:
                recipient_ids: List[str] = []
                proj = None
                db2 = SyncSessionLocal()
                try:
                    proj = db2.execute(
                        select(Project).where(Project.id == project_id)
                    ).scalar_one_or_none()
                    if proj:
                        member_rows = db2.execute(
                            select(ProjectMember.user_id).where(
                                ProjectMember.project_id == project_id
                            )
                        ).scalars().all()
                        recipient_ids = [
                            str(m) for m in member_rows if str(m) != str(user_id)
                        ]
                finally:
                    db2.close()

                if proj and recipient_ids:
                    sender_profile = self.files_service._get_user_profile(user_id)
                    sender_name = sender_profile.display_name or "Someone"
                    preview = (message_data.body or "")[:500]
                    trigger_project_chat_message_notification(
                        recipient_user_ids=recipient_ids,
                        project_id=project_id,
                        org_id=proj.org_id,
                        project_name=proj.name or "Project",
                        sender_id=user_id,
                        sender_name=sender_name,
                        message_preview=preview,
                    )
            except Exception as e:
                logger.error("Failed to send project chat inbox notification: %s", e, exc_info=True)

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
            db = SyncSessionLocal()
            try:
                base = select(ChatMessage).where(
                    ChatMessage.project_id == project_id,
                    ChatMessage.deleted_at.is_(None),
                )
                if search:
                    base = base.where(
                        ChatMessage.search_vector.op("@@")(
                            func.plainto_tsquery("english", search)
                        )
                    )
                if before_date:
                    base = base.where(ChatMessage.created_at < before_date)
                if after_date:
                    base = base.where(ChatMessage.created_at > after_date)

                count_stmt = select(func.count()).select_from(base.subquery())
                total = db.execute(count_stmt).scalar_one()

                ordered = base.order_by(ChatMessage.created_at.desc())
                lim, off, page_stmt = apply_sa_limit_offset(ordered, limit, offset)
                rows = db.execute(page_stmt).scalars().all()

                messages = []
                for m in rows:
                    messages.append(
                        {
                            "id": str(m.id),
                            "body": m.body,
                            "user_id": str(m.user_id),
                            "project_id": str(m.project_id),
                            "created_at": m.created_at.isoformat() if m.created_at else None,
                            "read_by": m.read_by,
                            "message_type": m.message_type,
                            "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
                            "attachments": m.attachments,
                            "reply_to_id": str(m.reply_to_id) if m.reply_to_id else None,
                            "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                        }
                    )
                limit, offset = lim, off
            finally:
                db.close()
            
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
                        user_ids.add(as_uuid(message['user_id']))
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
            db = SyncSessionLocal()
            try:
                if is_project_message:
                    msg = db.execute(
                        select(ChatMessage).where(ChatMessage.id == message_id)
                    ).scalar_one_or_none()
                    if not msg:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Message not found",
                        )
                    if msg.user_id != user_id:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="You can only edit your own messages",
                        )
                    created_at = msg.created_at
                    user_field = "user_id"
                else:
                    msg = db.execute(
                        select(DirectMessage).where(DirectMessage.id == message_id)
                    ).scalar_one_or_none()
                    if not msg:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Message not found",
                        )
                    if msg.sender_id != user_id:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="You can only edit your own messages",
                        )
                    created_at = msg.created_at
                    user_field = "sender_id"

                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - created_at > timedelta(hours=24):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Messages can only be edited within 24 hours",
                    )

                msg.body = message_data.body
                msg.edited_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(msg)

                if is_project_message:
                    updated_message = {
                        "id": str(msg.id),
                        "body": msg.body,
                        "user_id": str(msg.user_id),
                        "project_id": str(msg.project_id),
                        "created_at": msg.created_at.isoformat() if msg.created_at else None,
                        "read_by": msg.read_by,
                        "message_type": msg.message_type,
                        "deleted_at": msg.deleted_at.isoformat() if msg.deleted_at else None,
                        "attachments": msg.attachments,
                        "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
                        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
                    }
                    updated_message["read_by"] = self._normalize_read_by(
                        updated_message.get("read_by", [])
                    )
                    user_id_for_enrich = as_uuid(updated_message[user_field])
                else:
                    updated_message = {
                        "id": str(msg.id),
                        "body": msg.body,
                        "sender_id": str(msg.sender_id),
                        "receiver_id": str(msg.receiver_id),
                        "organization_id": str(msg.organization_id),
                        "created_at": msg.created_at.isoformat() if msg.created_at else None,
                        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
                        "deleted_at": msg.deleted_at.isoformat() if msg.deleted_at else None,
                        "message_type": msg.message_type,
                        "attachments": msg.attachments,
                        "read_at": msg.read_at.isoformat() if msg.read_at else None,
                    }
                    user_id_for_enrich = as_uuid(updated_message[user_field])

                self._enrich_message_with_user_info(updated_message, user_id_for_enrich)
                return updated_message
            except HTTPException:
                db.rollback()
                raise
            finally:
                db.close()
            
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
            db = SyncSessionLocal()
            try:
                if is_project_message:
                    msg = db.execute(
                        select(ChatMessage).where(ChatMessage.id == message_id)
                    ).scalar_one_or_none()
                    if not msg:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Message not found",
                        )
                    if msg.user_id != user_id and not is_project_admin:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="You can only delete your own messages",
                        )
                    msg.deleted_at = datetime.now(timezone.utc)
                else:
                    msg = db.execute(
                        select(DirectMessage).where(DirectMessage.id == message_id)
                    ).scalar_one_or_none()
                    if not msg:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Message not found",
                        )
                    if msg.sender_id != user_id and not is_project_admin:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="You can only delete your own messages",
                        )
                    msg.deleted_at = datetime.now(timezone.utc)
                db.commit()
            except HTTPException:
                db.rollback()
                raise
            finally:
                db.close()
            
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
            user_id_str = str(user_id)
            db = SyncSessionLocal()
            try:
                last_created = db.execute(
                    select(ChatMessage.created_at).where(ChatMessage.id == last_read_message_id)
                ).scalar_one_or_none()
                if not last_created:
                    return []

                id_rows = db.execute(
                    select(ChatMessage.id).where(
                        ChatMessage.project_id == project_id,
                        ChatMessage.created_at <= last_created,
                        ChatMessage.deleted_at.is_(None),
                    )
                ).scalars().all()
                message_ids = [str(i) for i in id_rows]
                if not message_ids:
                    return []

                db.execute(
                    text(
                        """
                        UPDATE chat_messages
                        SET read_by = CASE
                            WHEN read_by IS NULL THEN jsonb_build_array(:uid)
                            WHEN NOT (read_by @> to_jsonb(:uid::text)) THEN read_by || to_jsonb(:uid::text)
                            ELSE read_by
                        END
                        WHERE project_id = CAST(:pid AS uuid)
                          AND created_at <= :last_at
                          AND deleted_at IS NULL
                          AND (read_by IS NULL OR NOT (read_by @> to_jsonb(:uid::text)))
                        """
                    ),
                    {
                        "uid": user_id_str,
                        "pid": str(project_id),
                        "last_at": last_created,
                    },
                )

                proj = db.execute(
                    select(Project.org_id).where(Project.id == project_id)
                ).first()
                if proj and proj[0]:
                    org_id = proj[0]
                    now = datetime.now(timezone.utc)
                    upsert = (
                        pg_insert(ChatNotification)
                        .values(
                            user_id=user_id,
                            chat_type="project",
                            reference_id=project_id,
                            unread_count=0,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            constraint="uq_chat_notifications",
                            set_={"unread_count": 0, "updated_at": now},
                        )
                    )
                    db.execute(upsert)
                    logger.info(
                        "Updated chat_notifications: unread_count=0 for user %s, project %s",
                        user_id_str,
                        project_id,
                    )
                    cache_service.invalidate_pattern(
                        f"project_conversations:{user_id_str}:{org_id}:*"
                    )
                    cache_service.invalidate_pattern(
                        f"chat_notifications:project:{project_id}:{user_id_str}:*"
                    )

                db.commit()
                return message_ids
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            
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
            db = SyncSessionLocal()
            try:
                now = datetime.now(timezone.utc)
                exp = now + timedelta(seconds=5)
                if is_typing:
                    row = db.execute(
                        select(ChatTypingIndicator).where(
                            ChatTypingIndicator.chat_type == chat_type,
                            ChatTypingIndicator.reference_id == reference_id,
                            ChatTypingIndicator.user_id == user_id,
                        )
                    ).scalar_one_or_none()
                    if row:
                        row.started_at = now
                        row.expires_at = exp
                    else:
                        db.add(
                            ChatTypingIndicator(
                                chat_type=chat_type,
                                reference_id=reference_id,
                                user_id=user_id,
                                started_at=now,
                                expires_at=exp,
                            )
                        )
                else:
                    db.execute(
                        delete(ChatTypingIndicator).where(
                            ChatTypingIndicator.reference_id == reference_id,
                            ChatTypingIndicator.user_id == user_id,
                            ChatTypingIndicator.chat_type == chat_type,
                        )
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

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
            
            u1s = min(str(sender_id), str(receiver_id))
            u2s = max(str(sender_id), str(receiver_id))
            u1 = as_uuid(u1s)
            u2 = as_uuid(u2s)

            db = SyncSessionLocal()
            try:
                conv = db.execute(
                    select(ChatConversation).where(
                        ChatConversation.user1_id == u1,
                        ChatConversation.user2_id == u2,
                        ChatConversation.organization_id == organization_id,
                    )
                ).scalar_one_or_none()
                if not conv:
                    conv = ChatConversation(
                        user1_id=u1,
                        user2_id=u2,
                        organization_id=organization_id,
                        last_message_at=datetime.now(timezone.utc),
                    )
                    db.add(conv)
                    db.commit()
                    db.refresh(conv)

                conversation = {
                    "id": str(conv.id),
                    "user1_id": str(conv.user1_id),
                    "user2_id": str(conv.user2_id),
                    "organization_id": str(conv.organization_id),
                    "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                    "created_at": conv.created_at.isoformat() if conv.created_at else None,
                }

                try:
                    dm = db.execute(
                        select(DirectMessage)
                        .where(
                            DirectMessage.organization_id == organization_id,
                            DirectMessage.deleted_at.is_(None),
                            or_(
                                and_(
                                    DirectMessage.sender_id == u1,
                                    DirectMessage.receiver_id == u2,
                                ),
                                and_(
                                    DirectMessage.sender_id == u2,
                                    DirectMessage.receiver_id == u1,
                                ),
                            ),
                        )
                        .order_by(DirectMessage.created_at.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                    if dm:
                        body = dm.body or ""
                        if body:
                            conversation["last_message_preview"] = body[:100] + (
                                "..." if len(body) > 100 else ""
                            )
                        else:
                            conversation["last_message_preview"] = "[File attachment]"
                    else:
                        conversation["last_message_preview"] = None
                except Exception:
                    conversation["last_message_preview"] = None
            finally:
                db.close()

            self._enrich_conversation_with_user_info(conversation, sender_id)
            
            # Ensure unread_count is set
            if 'unread_count' not in conversation:
                conversation['unread_count'] = 0
            
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
            db = SyncSessionLocal()
            try:
                base = select(ChatConversation).where(
                    ChatConversation.organization_id == organization_id,
                    or_(
                        ChatConversation.user1_id == user_id,
                        ChatConversation.user2_id == user_id,
                    ),
                )
                total = db.execute(
                    select(func.count()).select_from(base.subquery())
                ).scalar_one()
                ordered = base.order_by(
                    ChatConversation.last_message_at.desc().nullslast(),
                    ChatConversation.created_at.desc(),
                )
                lim, off, page_stmt = apply_sa_limit_offset(ordered, limit, offset)
                conv_rows = db.execute(page_stmt).scalars().all()
                limit, offset = lim, off

                conversations = []
                conversation_ids = []
                user_ids = set()
                for c in conv_rows:
                    cid = str(c.id)
                    conversation_ids.append(cid)
                    d = {
                        "id": cid,
                        "user1_id": str(c.user1_id),
                        "user2_id": str(c.user2_id),
                        "organization_id": str(c.organization_id),
                        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                    }
                    conversations.append(d)
                    ou = (
                        d["user2_id"]
                        if d["user1_id"] == str(user_id)
                        else d["user1_id"]
                    )
                    if ou:
                        try:
                            user_ids.add(as_uuid(ou))
                        except Exception:
                            pass

                user_info_cache = {}
                if user_ids:
                    user_info_cache = self._batch_get_user_info(list(user_ids))

                unread_counts = {}
                if conversation_ids:
                    uuids = [as_uuid(x) for x in conversation_ids]
                    notif_rows = db.execute(
                        select(ChatNotification.reference_id, ChatNotification.unread_count).where(
                            ChatNotification.user_id == user_id,
                            ChatNotification.chat_type == "direct",
                            ChatNotification.reference_id.in_(uuids),
                        )
                    ).all()
                    for ref_id, uc in notif_rows:
                        unread_counts[str(ref_id)] = uc or 0

                last_messages = {}
                for d in conversations:
                    conv_id_str = d["id"]
                    try:
                        u1 = as_uuid(d["user1_id"])
                        u2 = as_uuid(d["user2_id"])
                        dm = db.execute(
                            select(DirectMessage)
                            .where(
                                DirectMessage.organization_id == organization_id,
                                DirectMessage.deleted_at.is_(None),
                                or_(
                                    and_(
                                        DirectMessage.sender_id == u1,
                                        DirectMessage.receiver_id == u2,
                                    ),
                                    and_(
                                        DirectMessage.sender_id == u2,
                                        DirectMessage.receiver_id == u1,
                                    ),
                                ),
                            )
                            .order_by(DirectMessage.created_at.desc())
                            .limit(1)
                        ).scalar_one_or_none()
                        if dm:
                            last_messages[conv_id_str] = {
                                "body": dm.body,
                                "created_at": dm.created_at.isoformat() if dm.created_at else None,
                                "sender_id": str(dm.sender_id),
                                "receiver_id": str(dm.receiver_id),
                            }
                    except Exception as e:
                        logger.warning(
                            "Failed to fetch last message for conversation %s: %s",
                            conv_id_str,
                            e,
                        )
            finally:
                db.close()

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
                
                # Set last message preview
                last_message = last_messages.get(conversation_id_str)
                if last_message:
                    body = last_message.get('body', '')
                    if body:
                        conversation['last_message_preview'] = body[:100] + ('...' if len(body) > 100 else '')
                    else:
                        conversation['last_message_preview'] = '[File attachment]'
                else:
                    conversation['last_message_preview'] = None
            
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
            db = SyncSessionLocal()
            try:
                projects = db.execute(
                    select(Project)
                    .join(ProjectMember, ProjectMember.project_id == Project.id)
                    .where(
                        ProjectMember.user_id == user_id,
                        Project.org_id == organization_id,
                        Project.archived.is_(False),
                    )
                ).scalars().all()

                if not projects:
                    return {
                        "conversations": [],
                        "total": 0,
                        "limit": limit,
                        "offset": offset,
                    }

                project_ids = [p.id for p in projects]
                unread_counts = {}
                if project_ids:
                    for ref_id, uc in db.execute(
                        select(
                            ChatNotification.reference_id,
                            ChatNotification.unread_count,
                        ).where(
                            ChatNotification.user_id == user_id,
                            ChatNotification.chat_type == "project",
                            ChatNotification.reference_id.in_(project_ids),
                        )
                    ).all():
                        unread_counts[str(ref_id)] = uc or 0

                conversations_data = []
                for project in projects:
                    project_id = project.id
                    project_id_str = str(project_id)

                    cm = db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.project_id == project_id,
                            ChatMessage.deleted_at.is_(None),
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .limit(1)
                    ).scalar_one_or_none()

                    last_message_at = None
                    last_message_preview = None
                    if cm and cm.created_at:
                        last_message_at = cm.created_at
                        if last_message_at.tzinfo is None:
                            last_message_at = last_message_at.replace(tzinfo=timezone.utc)
                        body = cm.body or ""
                        if body:
                            last_message_preview = body[:100] + (
                                "..." if len(body) > 100 else ""
                            )
                        else:
                            last_message_preview = "[File attachment]"

                    unread_count = unread_counts.get(project_id_str, 0)
                    avatar_url = None
                    if project.avatar_file_id:
                        avatar_url = self.files_service.get_file_url(project.avatar_file_id)

                    conversations_data.append(
                        {
                            "project_id": project_id,
                            "project_name": project.name,
                            "avatar_color": project.avatar_color,
                            "avatar_icon": project.avatar_icon,
                            "avatar_url": avatar_url,
                            "last_message_at": last_message_at,
                            "last_message_preview": last_message_preview,
                            "unread_count": unread_count,
                        }
                    )
            finally:
                db.close()
            
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
        message_data: DirectMessageCreate,
        organization_id: UUID4
    ) -> DirectMessageResponse:
        """
        Send a direct message
        
        Args:
            conversation_id: The conversation ID
            sender_id: The user sending the message
            message_data: The message data
            organization_id: The organization ID (must match conversation's organization)
            
        Returns:
            DirectMessageResponse: The created message
        """
        try:
            conversation = self._get_conversation(conversation_id, sender_id, organization_id)
            
            receiver_id = conversation['user2_id'] if conversation['user1_id'] == str(sender_id) else conversation['user1_id']
            
            message_type = MessageType.FILE if message_data.attachments else MessageType.TEXT
            
            att_list = None
            if message_data.attachments:
                att_list = [str(att_id) for att_id in message_data.attachments]

            db = SyncSessionLocal()
            try:
                dm = DirectMessage(
                    sender_id=sender_id,
                    receiver_id=as_uuid(receiver_id),
                    organization_id=as_uuid(conversation["organization_id"]),
                    body=message_data.body,
                    message_type=message_type.value,
                    attachments=att_list,
                )
                db.add(dm)
                db.commit()
                db.refresh(dm)
                message = {
                    "id": str(dm.id),
                    "sender_id": str(dm.sender_id),
                    "receiver_id": str(dm.receiver_id),
                    "organization_id": str(dm.organization_id),
                    "body": dm.body,
                    "message_type": dm.message_type,
                    "attachments": dm.attachments,
                    "created_at": dm.created_at.isoformat() if dm.created_at else None,
                    "edited_at": dm.edited_at.isoformat() if dm.edited_at else None,
                    "deleted_at": dm.deleted_at.isoformat() if dm.deleted_at else None,
                    "read_at": dm.read_at.isoformat() if dm.read_at else None,
                }
                message_id = message["id"]
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            if not message_id:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create message",
                )
            
            # Link attachments to the message if any
            if message_data.attachments:
                self._link_attachments_to_message(message_data.attachments, message_id, 'direct')
            
            self._enrich_dm_with_user_info(message, sender_id, str(receiver_id) if receiver_id else None)
            
            try:
                sender_profile = self.files_service._get_user_profile(sender_id)
                sender_name = sender_profile.display_name or 'Someone'
                message_preview = message_data.body[:100] if message_data.body else "Sent an attachment"
                
                trigger_direct_message_notification(
                    user_id=as_uuid(receiver_id),
                    org_id=as_uuid(conversation['organization_id']),
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
        organization_id: UUID4,
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
            organization_id: The organization ID (must match conversation's organization)
            limit: Number of messages to return
            offset: Number of messages to skip
            before_date: Get messages before this date
            after_date: Get messages after this date
            
        Returns:
            Dict containing messages and pagination info
        """
        try:
            conversation = self._get_conversation(conversation_id, user_id, organization_id)
            u1 = as_uuid(conversation["user1_id"])
            u2 = as_uuid(conversation["user2_id"])

            db = SyncSessionLocal()
            try:
                base = select(DirectMessage).where(
                    DirectMessage.organization_id == organization_id,
                    DirectMessage.deleted_at.is_(None),
                    or_(
                        and_(
                            DirectMessage.sender_id == u1,
                            DirectMessage.receiver_id == u2,
                        ),
                        and_(
                            DirectMessage.sender_id == u2,
                            DirectMessage.receiver_id == u1,
                        ),
                    ),
                )
                if before_date:
                    base = base.where(DirectMessage.created_at < before_date)
                if after_date:
                    base = base.where(DirectMessage.created_at > after_date)

                total = db.execute(
                    select(func.count()).select_from(base.subquery())
                ).scalar_one()
                ordered = base.order_by(DirectMessage.created_at.desc())
                lim, off, page_stmt = apply_sa_limit_offset(ordered, limit, offset)
                rows = db.execute(page_stmt).scalars().all()
                limit, offset = lim, off

                messages = []
                for m in rows:
                    messages.append(
                        {
                            "id": str(m.id),
                            "body": m.body,
                            "sender_id": str(m.sender_id),
                            "receiver_id": str(m.receiver_id),
                            "created_at": m.created_at.isoformat() if m.created_at else None,
                            "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                            "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
                            "message_type": m.message_type,
                            "attachments": m.attachments,
                            "read_at": m.read_at.isoformat() if m.read_at else None,
                            "organization_id": str(m.organization_id),
                        }
                    )
            finally:
                db.close()
            
            # Collect all unique sender and receiver IDs for batch fetching
            user_ids = set()
            for message in messages:
                if message.get('sender_id'):
                    try:
                        user_ids.add(as_uuid(message['sender_id']))
                    except Exception:
                        pass
                if message.get('receiver_id'):
                    try:
                        user_ids.add(as_uuid(message['receiver_id']))
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
                
                # Ensure edited_at is set (None if not present)
                if 'edited_at' not in message:
                    message['edited_at'] = None
                
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
        last_read_message_id: UUID4,
        organization_id: UUID4
    ) -> List[str]:
        """
        Mark direct messages as read
        
        Args:
            conversation_id: The conversation ID
            user_id: The user marking messages as read
            last_read_message_id: ID of the last message read
            organization_id: The organization ID (must match conversation's organization)
            
        Returns:
            List of message IDs that were marked as read
        """
        try:
            conversation = self._get_conversation(conversation_id, user_id, organization_id)
            other_user_id = (
                conversation["user2_id"]
                if conversation["user1_id"] == str(user_id)
                else conversation["user1_id"]
            )
            other_uuid = as_uuid(other_user_id)

            db = SyncSessionLocal()
            try:
                lm = db.execute(
                    select(DirectMessage.created_at).where(
                        DirectMessage.id == last_read_message_id
                    )
                ).scalar_one_or_none()
                if not lm:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Message not found",
                    )
                last_message_created_at = lm
                if not last_message_created_at:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Message missing created_at field",
                    )

                id_rows = db.execute(
                    select(DirectMessage.id).where(
                        DirectMessage.organization_id == organization_id,
                        DirectMessage.receiver_id == user_id,
                        DirectMessage.sender_id == other_uuid,
                        DirectMessage.created_at <= last_message_created_at,
                        DirectMessage.read_at.is_(None),
                    )
                ).scalars().all()
                message_ids = [str(i) for i in id_rows]

                if message_ids:
                    read_at = datetime.now(timezone.utc)
                    db.execute(
                        update(DirectMessage)
                        .where(
                            DirectMessage.organization_id == organization_id,
                            DirectMessage.receiver_id == user_id,
                            DirectMessage.sender_id == other_uuid,
                            DirectMessage.created_at <= last_message_created_at,
                            DirectMessage.read_at.is_(None),
                        )
                        .values(read_at=read_at)
                    )
                    now = datetime.now(timezone.utc)
                    db.execute(
                        pg_insert(ChatNotification)
                        .values(
                            user_id=user_id,
                            chat_type="direct",
                            reference_id=conversation_id,
                            unread_count=0,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            constraint="uq_chat_notifications",
                            set_={"unread_count": 0, "updated_at": now},
                        )
                    )
                    logger.info(
                        "Updated chat_notifications: unread_count=0 for user %s, conversation %s",
                        user_id,
                        conversation_id,
                    )
                    cache_service.invalidate_pattern(
                        f"direct_conversations:{str(user_id)}:{str(organization_id)}:*"
                    )
                    cache_service.invalidate_pattern(
                        f"chat_notifications:direct:{conversation_id}:{str(user_id)}:*"
                    )
                db.commit()
                return message_ids
            except HTTPException:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            
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
            
            if not chat_type or chat_type == "project":
                project_results = self._search_project_messages(
                    user_id, search_term, limit, offset
                )
                results.extend(project_results.get("messages", []))

            if not chat_type or chat_type == "direct":
                dm_results = self._search_direct_messages(
                    user_id, organization_id, search_term, limit, offset
                )
                results.extend(dm_results.get("messages", []))
            
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
            db = SyncSessionLocal()
            try:
                rows = db.execute(
                    select(
                        ChatNotification.unread_count,
                        ChatNotification.chat_type,
                        ChatNotification.reference_id,
                        ChatNotification.updated_at,
                    ).where(
                        ChatNotification.user_id == user_id,
                        ChatNotification.unread_count > 0,
                    )
                ).all()
            finally:
                db.close()

            notifications = [
                {
                    "unread_count": r[0],
                    "chat_type": r[1],
                    "reference_id": str(r[2]),
                    "updated_at": r[3].isoformat() if r[3] else None,
                }
                for r in rows
            ]
            
            project_chats = []
            direct_messages = []
            total_unread = 0
            
            for notif in notifications:
                total_unread += notif['unread_count']
                
                unread_count = UnreadCountResponse(
                    chat_type=notif["chat_type"],
                    reference_id=notif["reference_id"],
                    reference_name=self._get_reference_name(
                        notif["reference_id"], notif["chat_type"]
                    ),
                    unread_count=notif["unread_count"],
                    last_message_preview=None,
                    last_message_at=notif.get("updated_at"),
                )
                
                if notif["chat_type"] == "project":
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
            attachment_id_strings = [as_uuid(str(a)) for a in attachment_ids]
            mid = as_uuid(str(message_id))
            db = SyncSessionLocal()
            try:
                db.execute(
                    update(ChatAttachment)
                    .where(
                        ChatAttachment.id.in_(attachment_id_strings),
                        ChatAttachment.message_type == message_type,
                        ChatAttachment.message_id.is_(None),
                    )
                    .values(message_id=mid)
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            
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
        user_id = as_uuid(user_id)

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
            message['sender'] = self._get_user_info_with_cache(as_uuid(sender_id))
        else:
            message['sender'] = None
        
        # Get receiver info
        if receiver_id:
            message['receiver'] = self._get_user_info_with_cache(as_uuid(receiver_id))
        else:
            message['receiver'] = None
    
    def _enrich_conversation_with_user_info(self, conversation: Dict[str, Any], current_user_id: UUID4) -> None:
        """Add other user information to conversation with Redis caching"""
        try:
            other_user_id = conversation['user2_id'] if conversation['user1_id'] == str(current_user_id) else conversation['user1_id']
            
            if not other_user_id:
                conversation['other_user'] = None
                return
            
            conversation['other_user'] = self._get_user_info_with_cache(as_uuid(other_user_id))
            
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
            db = SyncSessionLocal()
            try:
                uc = db.execute(
                    select(ChatNotification.unread_count).where(
                        ChatNotification.user_id == user_id,
                        ChatNotification.chat_type == "direct",
                        ChatNotification.reference_id == as_uuid(str(conversation["id"])),
                    )
                ).scalar_one_or_none()
            finally:
                db.close()
            conversation["unread_count"] = uc if uc is not None else 0
        except Exception as e:
            logger.warning(f"Could not get unread count: {str(e)}")
            conversation['unread_count'] = 0
    
    def _get_conversation(self, conversation_id: UUID4, user_id: UUID4, organization_id: Optional[UUID4] = None) -> Dict[str, Any]:
        """Get and verify conversation access"""
        db = SyncSessionLocal()
        try:
            q = select(ChatConversation).where(ChatConversation.id == conversation_id)
            if organization_id:
                q = q.where(ChatConversation.organization_id == organization_id)
            conv = db.execute(q).scalar_one_or_none()
        finally:
            db.close()

        if not conv:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )

        conversation = {
            "id": str(conv.id),
            "user1_id": str(conv.user1_id),
            "user2_id": str(conv.user2_id),
            "organization_id": str(conv.organization_id),
            "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
        }

        if conversation["user1_id"] != str(user_id) and conversation["user2_id"] != str(user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this conversation",
            )

        if organization_id and str(conv.organization_id) != str(organization_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation does not belong to this organization",
            )

        return conversation
    
    def _verify_same_organization(self, user1_id: UUID4, user2_id: UUID4, organization_id: UUID4) -> None:
        """Verify both users belong to the same organization"""
        db = SyncSessionLocal()
        try:
            m1 = db.execute(
                select(OrganizationMember.id).where(
                    OrganizationMember.user_id == user1_id,
                    OrganizationMember.org_id == organization_id,
                )
            ).first()
            m2 = db.execute(
                select(OrganizationMember.id).where(
                    OrganizationMember.user_id == user2_id,
                    OrganizationMember.org_id == organization_id,
                )
            ).first()
        finally:
            db.close()

        if not m1 or not m2:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Both users must belong to the same organization"
            )

    def get_workspace_users(self, workspace_id: UUID4) -> List[Dict[str, Any]]:
        """Fetch all users in the workspace (organization). Selects only id, email, full_name, avatar_url."""
        db = SyncSessionLocal()
        try:
            uids = db.execute(
                select(OrganizationMember.user_id).where(
                    OrganizationMember.org_id == workspace_id
                )
            ).scalars().all()
            if not uids:
                return []
            uid_set = list({u for u in uids})
            profiles = db.execute(
                select(Profile).where(Profile.user_id.in_(uid_set))
            ).scalars().all()
        except Exception as e:
            logger.error(f"Failed to get workspace members: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to fetch workspace users",
            )
        finally:
            db.close()

        result = []
        for profile in profiles:
            avatar_url = None
            if profile.avatar_file_id:
                try:
                    avatar_url = self.files_service.get_file_url(profile.avatar_file_id)
                except Exception:
                    pass
            result.append(
                {
                    "id": str(profile.user_id),
                    "email": profile.email,
                    "full_name": profile.display_name,
                    "avatar_url": avatar_url,
                }
            )
        return result

    def _search_project_messages(self, user_id: UUID4, search_term: str, limit: int, offset: int) -> Dict[str, Any]:
        """Search project messages accessible to user"""
        try:
            db = SyncSessionLocal()
            try:
                base_pm = select(ProjectMember.project_id).where(
                    ProjectMember.user_id == user_id
                )
                lim, off, pm_stmt = apply_sa_limit_offset(base_pm, limit, offset)
                project_ids = list(db.execute(pm_stmt).scalars().all())
                limit, offset = lim, off

                results = []
                all_messages = []
                messages_count = 0
                if project_ids:
                    base_msg = select(ChatMessage).where(
                        ChatMessage.project_id.in_(project_ids),
                        ChatMessage.deleted_at.is_(None),
                        ChatMessage.search_vector.op("@@")(
                            func.plainto_tsquery("english", search_term)
                        ),
                    )
                    messages_count = db.execute(
                        select(func.count()).select_from(base_msg.subquery())
                    ).scalar_one()
                    rows = db.execute(
                        base_msg.order_by(ChatMessage.created_at.desc()).limit(limit)
                    ).scalars().all()
                    for m in rows:
                        all_messages.append(
                            {
                                "id": str(m.id),
                                "body": m.body,
                                "user_id": str(m.user_id),
                                "project_id": str(m.project_id),
                                "created_at": m.created_at.isoformat() if m.created_at else None,
                                "read_by": m.read_by,
                                "message_type": m.message_type,
                                "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
                                "attachments": m.attachments,
                                "reply_to_id": str(m.reply_to_id) if m.reply_to_id else None,
                                "edited_at": m.edited_at.isoformat() if m.edited_at else None,
                            }
                        )
            finally:
                db.close()

            user_ids = set()
            for msg in all_messages:
                if msg.get("user_id"):
                    try:
                        user_ids.add(as_uuid(msg["user_id"]))
                    except Exception:
                        pass

            user_info_cache = {}
            if user_ids:
                user_info_cache = self._batch_get_user_info(list(user_ids))

            for msg in all_messages:
                uid = msg.get("user_id")
                if uid:
                    user_id_str = str(uid)
                    msg["user"] = user_info_cache.get(user_id_str) or {
                        "id": user_id_str,
                        "display_name": None,
                        "avatar_url": None,
                    }
                else:
                    msg["user"] = None

                results.append(
                    {
                        "message_id": msg.get("id"),
                        "chat_type": "project",
                        "reference_id": msg.get("project_id"),
                        "body": msg.get("body"),
                        "user_id": msg.get("user_id"),
                        "user": msg.get("user"),
                        "created_at": msg.get("created_at"),
                        "relevance_score": 1.0,
                    }
                )

            return {
                "messages": results,
                "total": messages_count,
                "limit": limit,
                "offset": offset,
            }
        except Exception as e:
            logger.error(f"Error searching project messages: {str(e)}")
            return {"messages": [], "total": 0, "limit": limit, "offset": offset}
    
    def _search_direct_messages(self, user_id: UUID4, organization_id: UUID4, search_term: str, limit: int, offset: int) -> List[Dict]:
        """Search direct messages accessible to user"""
        try:
            db = SyncSessionLocal()
            try:
                base = select(DirectMessage).where(
                    DirectMessage.organization_id == organization_id,
                    DirectMessage.deleted_at.is_(None),
                    or_(
                        DirectMessage.sender_id == user_id,
                        DirectMessage.receiver_id == user_id,
                    ),
                    DirectMessage.search_vector.op("@@")(
                        func.plainto_tsquery("english", search_term)
                    ),
                )
                total = db.execute(
                    select(func.count()).select_from(base.subquery())
                ).scalar_one()
                lim, off, page = apply_sa_limit_offset(
                    base.order_by(DirectMessage.created_at.desc()), limit, offset
                )
                rows = db.execute(page).scalars().all()
                limit, offset = lim, off
                messages = []
                for m in rows:
                    messages.append(
                        {
                            "id": str(m.id),
                            "body": m.body,
                            "sender_id": str(m.sender_id),
                            "receiver_id": str(m.receiver_id),
                            "created_at": m.created_at.isoformat() if m.created_at else None,
                            "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
                            "message_type": m.message_type,
                            "attachments": m.attachments,
                            "read_at": m.read_at.isoformat() if m.read_at else None,
                            "organization_id": str(m.organization_id),
                        }
                    )
            finally:
                db.close()

            results = []
            
            # Collect all unique sender and receiver IDs for batch fetching
            user_ids = set()
            for msg in messages:
                if msg.get('sender_id'):
                    try:
                        user_ids.add(as_uuid(msg['sender_id']))
                    except Exception:
                        pass
                if msg.get('receiver_id'):
                    try:
                        user_ids.add(as_uuid(msg['receiver_id']))
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
                
                # Use .get() for safety in case keys are missing
                results.append({
                    'message_id': msg.get('id'),
                    'chat_type': 'direct',
                    'reference_id': msg.get('id'),
                    'body': msg.get('body'),
                    'user_id': msg.get('sender_id'),
                    'user': msg.get('sender'),
                    'created_at': msg.get('created_at'),
                    'relevance_score': 1.0
                })
            
            return {
                "messages": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        except Exception as e:
            logger.error(f"Error searching direct messages: {str(e)}")
            return {"messages": [], "total": 0, "limit": limit, "offset": offset}

    def _get_reference_name(self, reference_id: str, chat_type: str) -> str:
        """Get name for reference (project name or user name)"""
        try:
            db = SyncSessionLocal()
            try:
                if chat_type == "project":
                    name = db.execute(
                        select(Project.name).where(Project.id == as_uuid(str(reference_id)))
                    ).scalar_one_or_none()
                    return name or "Unknown Project"
                conv = db.execute(
                    select(ChatConversation.user2_id).where(
                        ChatConversation.id == as_uuid(str(reference_id))
                    )
                ).scalar_one_or_none()
            finally:
                db.close()
            if conv:
                user_info = self._get_user_info_with_cache(conv)
                return user_info.get("display_name") or "Unknown User"
            return "Unknown User"
        except Exception as e:
            logger.warning(f"Error getting reference name: {str(e)}")
            return "Unknown"

