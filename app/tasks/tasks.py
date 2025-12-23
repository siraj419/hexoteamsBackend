import asyncio
from app.core.celery import celery_app
from app.core.email import mailer

@celery_app.task(name='app.tasks.tasks.send_email_task')
def send_email_task(
    to_email: str, 
    subject: str,
    email_template: str, 
    body: str, 
    text_content: str, 
    token: str, 
    template_vars: dict
):
    """Send email via Celery task (async wrapper)."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            mailer.send_email(
                to_email,
                subject,
                email_template,
                text_content,
                token,
                template_vars
            )
        )
        loop.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send email to {to_email}: {str(e)}", exc_info=True)
        raise


@celery_app.task(name='app.tasks.tasks.send_organization_invitation_notification')
def send_organization_invitation_notification(
    user_id: str,
    org_id: str,
    org_name: str,
    inviter_id: str,
    inviter_name: str,
):
    """Send organization invitation notification via Celery."""
    try:
        from pydantic import UUID4
        from app.services.notification import NotificationService
        
        notification_service = NotificationService()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            notification_service.notify_organization_invitation(
                user_id=UUID4(user_id),
                org_id=UUID4(org_id),
                org_name=org_name,
                inviter_id=UUID4(inviter_id),
                inviter_name=inviter_name,
            )
        )
        loop.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send organization invitation notification: {str(e)}", exc_info=True)


@celery_app.task(name='app.tasks.tasks.send_task_assigned_notification')
def send_task_assigned_notification(
    user_id: str,
    org_id: str,
    task_id: str,
    task_title: str,
    assigned_by_id: str,
    assigned_by_name: str,
    project_name: str,
):
    """Send task assigned notification via Celery."""
    try:
        from pydantic import UUID4
        from app.services.notification import NotificationService
        
        notification_service = NotificationService()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            notification_service.notify_task_assigned(
                user_id=UUID4(user_id),
                org_id=UUID4(org_id),
                task_id=UUID4(task_id),
                task_title=task_title,
                assigned_by_id=UUID4(assigned_by_id),
                assigned_by_name=assigned_by_name,
                project_name=project_name,
            )
        )
        loop.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send task assigned notification: {str(e)}", exc_info=True)


@celery_app.task(name='app.tasks.tasks.send_task_unassigned_notification')
def send_task_unassigned_notification(
    user_id: str,
    org_id: str,
    task_id: str,
    task_title: str,
    unassigned_by_id: str,
    unassigned_by_name: str,
    project_name: str,
):
    """Send task unassigned notification via Celery."""
    try:
        from pydantic import UUID4
        from app.services.notification import NotificationService
        
        notification_service = NotificationService()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            notification_service.notify_task_unassigned(
                user_id=UUID4(user_id),
                org_id=UUID4(org_id),
                task_id=UUID4(task_id),
                task_title=task_title,
                unassigned_by_id=UUID4(unassigned_by_id),
                unassigned_by_name=unassigned_by_name,
                project_name=project_name,
            )
        )
        loop.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send task unassigned notification: {str(e)}", exc_info=True)


@celery_app.task(name='app.tasks.tasks.send_direct_message_notification')
def send_direct_message_notification(
    user_id: str,
    org_id: str,
    sender_id: str,
    sender_name: str,
    message_preview: str,
    conversation_id: str,
):
    """Send direct message notification via Celery."""
    try:
        from pydantic import UUID4
        from app.services.notification import NotificationService
        
        notification_service = NotificationService()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            notification_service.notify_direct_message(
                user_id=UUID4(user_id),
                org_id=UUID4(org_id),
                sender_id=UUID4(sender_id),
                sender_name=sender_name,
                message_preview=message_preview,
                conversation_id=UUID4(conversation_id),
            )
        )
        loop.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send direct message notification: {str(e)}", exc_info=True)


@celery_app.task(name='app.tasks.tasks.send_task_completed_notification')
def send_task_completed_notification(
    project_id: str,
    org_id: str,
    task_id: str,
    task_title: str,
    completed_by_id: str,
    completed_by_name: str,
    project_name: str,
):
    """Send task completed notification via Celery."""
    try:
        from pydantic import UUID4
        from app.services.notification import NotificationService
        
        notification_service = NotificationService()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            notification_service.notify_task_completed(
                project_id=UUID4(project_id),
                org_id=UUID4(org_id),
                task_id=UUID4(task_id),
                task_title=task_title,
                completed_by_id=UUID4(completed_by_id),
                completed_by_name=completed_by_name,
                project_name=project_name,
            )
        )
        loop.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send task completed notification: {str(e)}", exc_info=True)


@celery_app.task(name='app.tasks.tasks.send_project_member_added_notification')
def send_project_member_added_notification(
    user_id: str,
    org_id: str,
    project_id: str,
    project_name: str,
    added_by_id: str,
    added_by_name: str,
):
    """Send project member added notification via Celery."""
    try:
        from pydantic import UUID4
        from app.services.notification import NotificationService
        
        notification_service = NotificationService()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            notification_service.notify_project_member_added(
                user_id=UUID4(user_id),
                org_id=UUID4(org_id),
                project_id=UUID4(project_id),
                project_name=project_name,
                added_by_id=UUID4(added_by_id),
                added_by_name=added_by_name,
            )
        )
        loop.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to send project member added notification: {str(e)}", exc_info=True)