from pydantic import UUID4
from typing import Optional
import logging

from app.tasks.tasks import (
    send_organization_invitation_notification,
    send_task_assigned_notification,
    send_task_unassigned_notification,
    send_direct_message_notification,
    send_task_completed_notification,
)

logger = logging.getLogger(__name__)


def trigger_organization_invitation_notification(
    user_id: UUID4,
    org_id: UUID4,
    org_name: str,
    inviter_id: UUID4,
    inviter_name: str,
):
    """Helper to trigger organization invitation notification via Celery"""
    try:
        send_organization_invitation_notification.delay(
            user_id=str(user_id),
            org_id=str(org_id),
            org_name=org_name,
            inviter_id=str(inviter_id),
            inviter_name=inviter_name,
        )
    except Exception as e:
        logger.error(f"Failed to trigger organization invitation notification: {e}")


def trigger_task_assigned_notification(
    user_id: UUID4,
    org_id: UUID4,
    task_id: UUID4,
    task_title: str,
    assigned_by_id: UUID4,
    assigned_by_name: str,
    project_name: str,
):
    """Helper to trigger task assignment notification via Celery"""
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
        logger.error(f"Failed to trigger task assigned notification: {e}")


def trigger_task_unassigned_notification(
    user_id: UUID4,
    org_id: UUID4,
    task_id: UUID4,
    task_title: str,
    unassigned_by_id: UUID4,
    unassigned_by_name: str,
    project_name: str,
):
    """Helper to trigger task unassignment notification via Celery"""
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
        logger.error(f"Failed to trigger task unassigned notification: {e}")


def trigger_direct_message_notification(
    user_id: UUID4,
    org_id: UUID4,
    sender_id: UUID4,
    sender_name: str,
    message_preview: str,
    conversation_id: UUID4,
):
    """Helper to trigger direct message notification via Celery"""
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
        logger.error(f"Failed to trigger direct message notification: {e}")


def trigger_task_completed_notification(
    project_id: UUID4,
    org_id: UUID4,
    task_id: UUID4,
    task_title: str,
    completed_by_id: UUID4,
    completed_by_name: str,
    project_name: str,
):
    """Helper to trigger task completion notification via Celery"""
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
        logger.error(f"Failed to trigger task completed notification: {e}")

