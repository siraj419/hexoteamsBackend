from fastapi import HTTPException, status
from pydantic import UUID4
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import logging
import asyncio

from app.schemas.inbox import InboxEventType, InboxResponse
from app.services.inbox import InboxService
from app.utils.redis_cache import cache_service
from app.utils.notification_pubsub import publish_notification_event
from app.core import supabase
from app.core.config import Settings
from supabase_auth.errors import AuthApiError

logger = logging.getLogger(__name__)
settings = Settings()


class NotificationService:
    CACHE_TTL_PREFERENCES = 600  # 10 minutes
    
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
        send_browser: bool = True,
    ) -> InboxResponse:
        """
        Send inbox notification with optional browser notifications
        
        Args:
            title: Notification title
            message: Notification message
            user_id: User to notify
            org_id: Organization ID
            user_by: User who triggered the notification
            event_type: Type of event that triggered the notification
            reference_id: Optional reference ID (task_id, project_id, etc.)
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
        
        logger.info(f"User preferences: {user_preferences}")
        
        # if send_browser and user_preferences.get('browser_notifications', False):
        await self._send_browser_notification(
            user_id=user_id,
            org_id=org_id,
            inbox_data=inbox_response,
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
        
        inbox_response = await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=inviter_id,
            event_type=InboxEventType.ORGANIZATION_INVITATION,
            reference_id=org_id,
        )
        
        # Note: Organization invitation emails are already sent by TeamService
        # when creating invitations, so we skip email here to avoid duplicates
        
        return inbox_response
    
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
        
        inbox_response = await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=assigned_by_id,
            event_type=InboxEventType.TASK_ASSIGNED,
            reference_id=task_id,
        )
        
        # Send email notification
        try:
            org_name = self._get_organization_name(org_id)
            task_url = f"{settings.FRONTEND_URL}/tasks/{task_id}"
            template_vars = {
                'task_title': task_title,
                'project_name': project_name,
                'assigned_by_name': assigned_by_name,
                'task_url': task_url,
                'org_name': org_name,
                'due_date_line': '',  # Could be fetched if needed
            }
            
            self._send_email_notification(
                user_id=user_id,
                org_id=org_id,
                event_type=InboxEventType.TASK_ASSIGNED,
                template_name='task_assigned.html',
                subject=f"Task Assigned: {task_title}",
                template_vars=template_vars,
                text_content=f"{assigned_by_name} assigned you a task '{task_title}' in project {project_name}. View task: {task_url}",
            )
        except Exception as e:
            logger.error(f"Failed to send email for task assignment: {e}", exc_info=True)
        
        return inbox_response
    
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
        
        inbox_response = await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=unassigned_by_id,
            event_type=InboxEventType.TASK_UNASSIGNED,
            reference_id=task_id,
        )
        
        # Send email notification
        try:
            org_name = self._get_organization_name(org_id)
            task_url = f"{settings.FRONTEND_URL}/tasks/{task_id}"
            template_vars = {
                'task_title': task_title,
                'project_name': project_name,
                'unassigned_by_name': unassigned_by_name,
                'task_url': task_url,
                'org_name': org_name,
            }
            
            self._send_email_notification(
                user_id=user_id,
                org_id=org_id,
                event_type=InboxEventType.TASK_UNASSIGNED,
                template_name='task_unassigned.html',
                subject=f"Task Unassigned: {task_title}",
                template_vars=template_vars,
                text_content=f"{unassigned_by_name} unassigned you from task '{task_title}' in project {project_name}.",
            )
        except Exception as e:
            logger.error(f"Failed to send email for task unassignment: {e}", exc_info=True)
        
        return inbox_response
    
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
        message = f"{message_preview[:100]}"
        
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
            task_url = f"{settings.FRONTEND_URL}/tasks/{task_id}"
            org_name = self._get_organization_name(org_id)
            
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
                
                # Send email notification
                try:
                    template_vars = {
                        'task_title': task_title,
                        'project_name': project_name,
                        'completed_by_name': completed_by_name,
                        'task_url': task_url,
                        'org_name': org_name,
                    }
                    
                    self._send_email_notification(
                        user_id=UUID4(member_user_id),
                        org_id=org_id,
                        event_type=InboxEventType.TASK_COMPLETED,
                        template_name='task_completed.html',
                        subject=f"Task Completed: {task_title}",
                        template_vars=template_vars,
                        text_content=f"{completed_by_name} completed task '{task_title}' in project {project_name}. View task: {task_url}",
                    )
                except Exception as e:
                    logger.error(f"Failed to send email for task completion to user {member_user_id}: {e}", exc_info=True)
            
            return notifications
            
        except AuthApiError as e:
            logger.error(f"Failed to get project members: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to notify project members: {e}"
            )
    
    async def notify_project_member_added(
        self,
        user_id: UUID4,
        org_id: UUID4,
        project_id: UUID4,
        project_name: str,
        added_by_id: UUID4,
        added_by_name: str,
    ):
        """Notify user about being added to a project"""
        title = f"Added to Project: {project_name}"
        message = f"{added_by_name} added you as a member to the project '{project_name}'"
        
        inbox_response = await self.send_inbox_notification(
            title=title,
            message=message,
            user_id=user_id,
            org_id=org_id,
            user_by=added_by_id,
            event_type=InboxEventType.PROJECT_MEMBER_ADDED,
            reference_id=project_id,
        )
        
        # Send email notification
        try:
            org_name = self._get_organization_name(org_id)
            project_url = f"{settings.FRONTEND_URL}/projects/{project_id}"
            template_vars = {
                'org_name': org_name,
                'project_names': project_name,
                'inviter_name': added_by_name,
                'frontend_url': settings.FRONTEND_URL,
            }
            
            self._send_email_notification(
                user_id=user_id,
                org_id=org_id,
                event_type=InboxEventType.PROJECT_MEMBER_ADDED,
                template_name='project_addition.html',
                subject=f"You've been added to project {project_name}",
                template_vars=template_vars,
                text_content=f"{added_by_name} added you as a member to the project '{project_name}'. Visit {project_url} to view the project.",
            )
        except Exception as e:
            logger.error(f"Failed to send email for project member addition: {e}", exc_info=True)
        
        return inbox_response
    
    async def _send_browser_notification(
        self,
        user_id: UUID4,
        org_id: UUID4,
        inbox_data: InboxResponse,
    ):
        """Publish browser notification event to Redis Pub/Sub"""
        try:
            unread_count = self.inbox_service.get_unread_count(user_id, org_id)
            
            payload = {
                "data": inbox_data.model_dump(mode='json'),
                "unread_count": unread_count,
            }
            
            publish_notification_event(
                user_id=str(user_id),
                org_id=str(org_id),
                notification_type="inbox_new",
                payload=payload
            )
            
            logger.info(f"Browser notification event published for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to publish browser notification event: {e}")
    
    def _get_user_email(self, user_id: UUID4) -> Optional[str]:
        """Get user email from profile"""
        try:
            response = supabase.table('profiles').select('email').eq('user_id', str(user_id)).execute()
            if response.data and len(response.data) > 0:
                return response.data[0].get('email')
        except Exception as e:
            logger.error(f"Failed to get user email for {user_id}: {e}")
        return None
    
    def _get_organization_name(self, org_id: UUID4) -> str:
        """Get organization name from org_id with caching"""
        cache_key = f"organization:name:{org_id}"
        cached = cache_service.get(cache_key)
        if cached:
            return cached
        
        try:
            response = supabase.table('organizations').select('name').eq('id', str(org_id)).execute()
            if response.data and len(response.data) > 0:
                org_name = response.data[0].get('name', '')
                cache_service.set(cache_key, org_name, ttl=self.CACHE_TTL_PREFERENCES)
                return org_name
        except Exception as e:
            logger.error(f"Failed to get organization name for {org_id}: {e}")
        
        return ''
    
    def _get_user_preferences(self, user_id: UUID4) -> Dict[str, Any]:
        """Get user notification preferences from profile"""
        cache_key = f"user:preferences:{user_id}"
        
        # Check cache first
        cached = cache_service.get(cache_key)
        if cached:
            return cached
        
        try:
            response = supabase.table('profiles').select(
                'browser_notifications'
            ).eq('user_id', str(user_id)).execute()
            
            if not response.data or len(response.data) == 0:
                default_prefs = {
                    'browser_notifications': True,
                }
                cache_service.set(cache_key, default_prefs, ttl=self.CACHE_TTL_PREFERENCES)
                return default_prefs
            
            prefs = {
                'browser_notifications': response.data[0].get('browser_notifications', True),
            }
            cache_service.set(cache_key, prefs, ttl=self.CACHE_TTL_PREFERENCES)
            return prefs
        except Exception as e:
            logger.error(f"Failed to get user preferences: {e}")
            default_prefs = {
                'browser_notifications': True,
            }
            return default_prefs
    
    def _send_email_notification(
        self,
        user_id: UUID4,
        org_id: UUID4,
        event_type: InboxEventType,
        template_name: str,
        subject: str,
        template_vars: Dict[str, Any],
        text_content: str,
    ):
        """Send email notification via Celery task"""
        # Skip email for direct messages
        if event_type == InboxEventType.DIRECT_MESSAGE:
            return
        
        try:
            user_email = self._get_user_email(user_id)
            if not user_email:
                logger.warning(f"No email found for user {user_id}, skipping email notification")
                return
            
            from app.tasks.tasks import send_email_task
            
            send_email_task.delay(
                to_email=user_email,
                subject=subject,
                email_template=template_name,
                body='',
                text_content=text_content,
                token='',
                template_vars=template_vars
            )
            logger.info(f"Email notification queued for user {user_id} ({user_email})")
        except Exception as e:
            logger.error(f"Failed to queue email notification for user {user_id}: {e}", exc_info=True)

