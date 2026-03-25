from pydantic import UUID4
from typing import Optional
import logging
import asyncio

from app.tasks.tasks import (
    send_organization_invitation_notification,
    send_task_assigned_notification,
    send_task_unassigned_notification,
    send_direct_message_notification,
    send_task_completed_notification,
    send_project_member_added_notification,
)

logger = logging.getLogger(__name__)


def run_async_task(coro):
    """Helper to run async tasks synchronously"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If loop is already running, create a new one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(coro)
            loop.close()
            return result
        else:
            return loop.run_until_complete(coro)
    except Exception:
        # Fallback: create new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(coro)
        loop.close()
        return result


def trigger_organization_invitation_notification(
    user_id: UUID4,
    org_id: UUID4,
    org_name: str,
    inviter_id: UUID4,
    inviter_name: str,
):
    """Helper to trigger organization invitation notification via Celery with fallback"""
    try:
        send_organization_invitation_notification.delay(
            user_id=str(user_id),
            org_id=str(org_id),
            org_name=org_name,
            inviter_id=str(inviter_id),
            inviter_name=inviter_name,
        )
    except Exception as e:
        logger.warning(f"Celery task failed, using direct call: {e}")
        try:
            from app.services.notification import NotificationService
            notification_service = NotificationService()
            run_async_task(
                notification_service.notify_organization_invitation(
                    user_id=user_id,
                    org_id=org_id,
                    org_name=org_name,
                    inviter_id=inviter_id,
                    inviter_name=inviter_name,
                )
            )
        except Exception as fallback_error:
            logger.error(f"Failed to send organization invitation notification: {fallback_error}", exc_info=True)


def trigger_task_assigned_notification(
    user_id: UUID4,
    org_id: UUID4,
    task_id: UUID4,
    task_title: str,
    assigned_by_id: UUID4,
    assigned_by_name: str,
    project_name: str,
):
    """Helper to trigger task assignment notification via Celery with fallback"""
    try:
        send_task_assigned_notification.delay(
            user_id=str(user_id),
            org_id=str(org_id),
            task_id=str(task_id),
            task_title=task_title,
            assigned_by_id=str(assigned_by_id),
            assigned_by_name=assigned_by_name,
            project_name=project_name,
        )
    except Exception as e:
        logger.warning(f"Celery task failed, using direct call: {e}")
        try:
            from app.services.notification import NotificationService
            notification_service = NotificationService()
            run_async_task(
                notification_service.notify_task_assigned(
                    user_id=user_id,
                    org_id=org_id,
                    task_id=task_id,
                    task_title=task_title,
                    assigned_by_id=assigned_by_id,
                    assigned_by_name=assigned_by_name,
                    project_name=project_name,
                )
            )
        except Exception as fallback_error:
            logger.error(f"Failed to send task assigned notification: {fallback_error}", exc_info=True)


def trigger_task_unassigned_notification(
    user_id: UUID4,
    org_id: UUID4,
    task_id: UUID4,
    task_title: str,
    unassigned_by_id: UUID4,
    unassigned_by_name: str,
    project_name: str,
):
    """Helper to trigger task unassignment notification via Celery with fallback"""
    try:
        send_task_unassigned_notification.delay(
            user_id=str(user_id),
            org_id=str(org_id),
            task_id=str(task_id),
            task_title=task_title,
            unassigned_by_id=str(unassigned_by_id),
            unassigned_by_name=unassigned_by_name,
            project_name=project_name,
        )
    except Exception as e:
        logger.warning(f"Celery task failed, using direct call: {e}")
        try:
            from app.services.notification import NotificationService
            notification_service = NotificationService()
            run_async_task(
                notification_service.notify_task_unassigned(
                    user_id=user_id,
                    org_id=org_id,
                    task_id=task_id,
                    task_title=task_title,
                    unassigned_by_id=unassigned_by_id,
                    unassigned_by_name=unassigned_by_name,
                    project_name=project_name,
                )
            )
        except Exception as fallback_error:
            logger.error(f"Failed to send task unassigned notification: {fallback_error}", exc_info=True)


def trigger_direct_message_notification(
    user_id: UUID4,
    org_id: UUID4,
    sender_id: UUID4,
    sender_name: str,
    message_preview: str,
    conversation_id: UUID4,
):
    """Helper to trigger direct message notification via Celery with fallback"""
    try:
        send_direct_message_notification.delay(
            user_id=str(user_id),
            org_id=str(org_id),
            sender_id=str(sender_id),
            sender_name=sender_name,
            message_preview=message_preview,
            conversation_id=str(conversation_id),
        )
    except Exception as e:
        logger.warning(f"Celery task failed, using direct call: {e}")
        try:
            from app.services.notification import NotificationService
            notification_service = NotificationService()
            run_async_task(
                notification_service.notify_direct_message(
                    user_id=user_id,
                    org_id=org_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    message_preview=message_preview,
                    conversation_id=conversation_id,
                )
            )
        except Exception as fallback_error:
            logger.error(f"Failed to send direct message notification: {fallback_error}", exc_info=True)


def trigger_task_completed_notification(
    project_id: UUID4,
    org_id: UUID4,
    task_id: UUID4,
    task_title: str,
    completed_by_id: UUID4,
    completed_by_name: str,
    project_name: str,
):
    """Helper to trigger task completion notification via Celery with fallback"""
    try:
        send_task_completed_notification.delay(
            project_id=str(project_id),
            org_id=str(org_id),
            task_id=str(task_id),
            task_title=task_title,
            completed_by_id=str(completed_by_id),
            completed_by_name=completed_by_name,
            project_name=project_name,
        )
    except Exception as e:
        logger.warning(f"Celery task failed, using direct call: {e}")
        try:
            from app.services.notification import NotificationService
            notification_service = NotificationService()
            run_async_task(
                notification_service.notify_task_completed(
                    project_id=project_id,
                    org_id=org_id,
                    task_id=task_id,
                    task_title=task_title,
                    completed_by_id=completed_by_id,
                    completed_by_name=completed_by_name,
                    project_name=project_name,
                )
            )
        except Exception as fallback_error:
            logger.error(f"Failed to send task completed notification: {fallback_error}", exc_info=True)


def trigger_project_member_added_notification(
    user_id: UUID4,
    org_id: UUID4,
    project_id: UUID4,
    project_name: str,
    added_by_id: UUID4,
    added_by_name: str,
):
    """Helper to trigger project member added notification via Celery with fallback"""
    try:
        send_project_member_added_notification.delay(
            user_id=str(user_id),
            org_id=str(org_id),
            project_id=str(project_id),
            project_name=project_name,
            added_by_id=str(added_by_id),
            added_by_name=added_by_name,
        )
    except Exception as e:
        logger.warning(f"Celery task failed, using direct call: {e}")
        try:
            from app.services.notification import NotificationService
            notification_service = NotificationService()
            run_async_task(
                notification_service.notify_project_member_added(
                    user_id=user_id,
                    org_id=org_id,
                    project_id=project_id,
                    project_name=project_name,
                    added_by_id=added_by_id,
                    added_by_name=added_by_name,
                )
            )
        except Exception as fallback_error:
            logger.error(f"Failed to send project member added notification: {fallback_error}", exc_info=True)

