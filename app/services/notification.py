from fastapi import HTTPException, status
from pydantic import UUID4
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import logging
import asyncio

from app.schemas.inbox import InboxEventType, InboxResponse
from app.services.inbox import InboxService
from app.utils.websocket_manager import manager
from app.core import supabase
from app.tasks.tasks import send_email_task
from supabase_auth.errors import AuthApiError

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self):
        self.inbox_service = InboxService()
    
    async def send_inbox_notification(
        self,
        title: str,
        message: str,
        user_id: UUID4,
        org_id: UUID4,
        user_by: UUID4,
        event_type: InboxEventType,
        reference_id: Optional[UUID4] = None,
        send_email: bool = True,
        send_browser: bool = True,
    ) -> InboxResponse:
        """
        Send inbox notification with optional email and browser notifications
        
        Args:
            title: Notification title
            message: Notification message
            user_id: User to notify
            org_id: Organization ID
            user_by: User who triggered the notification
            event_type: Type of event that triggered the notification
            reference_id: Optional reference ID (task_id, project_id, etc.)
            send_email: Whether to send email notification
            send_browser: Whether to send browser notification
        
        Returns:
            InboxResponse: Created inbox notification
        """
        logger.info(f"Sending inbox notification: {title} to user {user_id}")
        
        try:
            inbox_response = self.inbox_service.create_inbox(
                title=title,
                message=message,
                user_id=user_id,
                org_id=org_id,
                user_by=user_by,
                event_type=event_type,
                reference_id=reference_id,
            )
            logger.info(f"Inbox notification created with ID: {inbox_response.id}")
        except Exception as e:
            logger.error(f"Failed to create inbox notification: {e}", exc_info=True)
            raise
        
        user_preferences = self._get_user_preferences(user_id)
        
        if send_browser and user_preferences.get('browser_notifications', False):
            await self._send_browser_notification(
                user_id=user_id,
                org_id=org_id,
                inbox_data=inbox_response,
            )
        
        if send_email and user_preferences.get('email_notifications', False):
            self._send_email_notification(
                user_id=user_id,
                title=title,
                message=message,
                event_type=event_type,
            )
        
        return inbox_response
    
    async def notify_organization_invitation(
        self,
        user_id: UUID4,
        org_id: UUID4,
        org_name: str,
        inviter_id: UUID4,
        inviter_name: str,
    ):
        """Notify user about organization invitation"""
        title = f"Invitation to {org_name}"
        message = f"{inviter_name} has invited you to join {org_name}"
        
        return await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=inviter_id,
            event_type=InboxEventType.ORGANIZATION_INVITATION,
            reference_id=org_id,
        )
    
    async def notify_task_assigned(
        self,
        user_id: UUID4,
        org_id: UUID4,
        task_id: UUID4,
        task_title: str,
        assigned_by_id: UUID4,
        assigned_by_name: str,
        project_name: str,
    ):
        """Notify user about task assignment"""
        title = f"Task Assigned: {task_title}"
        message = f"{assigned_by_name} assigned you a task '{task_title}' in project {project_name}"
        
        return await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=assigned_by_id,
            event_type=InboxEventType.TASK_ASSIGNED,
            reference_id=task_id,
        )
    
    async def notify_task_unassigned(
        self,
        user_id: UUID4,
        org_id: UUID4,
        task_id: UUID4,
        task_title: str,
        unassigned_by_id: UUID4,
        unassigned_by_name: str,
        project_name: str,
    ):
        """Notify user about task unassignment"""
        title = f"Task Unassigned: {task_title}"
        message = f"{unassigned_by_name} unassigned you from task '{task_title}' in project {project_name}"
        
        return await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=unassigned_by_id,
            event_type=InboxEventType.TASK_UNASSIGNED,
            reference_id=task_id,
        )
    
    async def notify_direct_message(
        self,
        user_id: UUID4,
        org_id: UUID4,
        sender_id: UUID4,
        sender_name: str,
        message_preview: str,
        conversation_id: UUID4,
    ):
        """Notify user about new direct message"""
        title = f"New message from {sender_name}"
        message = f"{sender_name}: {message_preview[:100]}"
        
        return await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=sender_id,
            event_type=InboxEventType.DIRECT_MESSAGE,
            reference_id=conversation_id,
        )
    
    async def notify_task_completed(
        self,
        project_id: UUID4,
        org_id: UUID4,
        task_id: UUID4,
        task_title: str,
        completed_by_id: UUID4,
        completed_by_name: str,
        project_name: str,
    ):
        """Notify all project members about task completion"""
        try:
            response = supabase.table('project_members').select('user_id').eq('project_id', str(project_id)).execute()
            
            if not response.data:
                logger.warning(f"No project members found for project {project_id}")
                return
            
            title = f"Task Completed: {task_title}"
            message = f"{completed_by_name} completed task '{task_title}' in project {project_name}"
            
            notifications = []
            for member in response.data:
                member_user_id = member['user_id']
                
                if str(member_user_id) == str(completed_by_id):
                    continue
                
                notification = await self.send_inbox_notification(
                    title=title,
                    message=message,
                    user_id=UUID4(member_user_id),
                    org_id=org_id,
                    user_by=completed_by_id,
                    event_type=InboxEventType.TASK_COMPLETED,
                    reference_id=task_id,
                )
                notifications.append(notification)
            
            return notifications
            
        except AuthApiError as e:
            logger.error(f"Failed to get project members: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to notify project members: {e}"
            )
    
    async def _send_browser_notification(
        self,
        user_id: UUID4,
        org_id: UUID4,
        inbox_data: InboxResponse,
    ):
        """Send browser notification via WebSocket"""
        try:
            unread_count = self.inbox_service.get_unread_count(user_id, org_id)
            
            await manager.broadcast_inbox_notification(
                org_id=str(org_id),
                user_id=str(user_id),
                message={
                    "type": "inbox_new",
                    "data": inbox_data.model_dump(mode='json'),
                    "unread_count": unread_count,
                }
            )
            
            logger.info(f"Browser notification sent to user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send browser notification: {e}")
    
    def _send_email_notification(
        self,
        user_id: UUID4,
        title: str,
        message: str,
        event_type: InboxEventType,
    ):
        """Send email notification"""
        try:
            from app.core.config import Settings
            settings = Settings()
            
            user_email = self._get_user_email(user_id)
            
            if not user_email:
                logger.warning(f"No email found for user {user_id}")
                return
            
            subject = f"[Notification] {title}"
            
            template_vars = {
                'title': title,
                'message': message,
                'event_type': event_type.value,
                'frontend_url': settings.FRONTEND_URL,
            }
            
            send_email_task.delay(
                to_email=user_email,
                subject=subject,
                email_template='inbox_notification.html',
                body='',
                text_content=message,
                token='',
                template_vars=template_vars
            )
            
            logger.info(f"Email notification queued for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send email notification: {e}")
    
    def _get_user_preferences(self, user_id: UUID4) -> Dict[str, Any]:
        """Get user notification preferences from profile"""
        try:
            response = supabase.table('profiles').select(
                'browser_notifications, email_notifications'
            ).eq('user_id', str(user_id)).execute()
            
            if not response.data or len(response.data) == 0:
                return {
                    'browser_notifications': True,
                    'email_notifications': True,
                }
            
            return {
                'browser_notifications': response.data[0].get('browser_notifications', True),
                'email_notifications': response.data[0].get('email_notifications', True),
            }
        except Exception as e:
            logger.error(f"Failed to get user preferences: {e}")
            return {
                'browser_notifications': True,
                'email_notifications': True,
            }
    
    def _get_user_email(self, user_id: UUID4) -> Optional[str]:
        """Get user email from profile"""
        try:
            response = supabase.table('profiles').select('email').eq('user_id', str(user_id)).execute()
            
            if not response.data or len(response.data) == 0:
                return None
            
            return response.data[0].get('email')
        except Exception as e:
            logger.error(f"Failed to get user email: {e}")
            return None

