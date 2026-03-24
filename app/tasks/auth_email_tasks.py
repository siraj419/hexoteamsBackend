"""Auth-related emails (registered as Celery tasks; keep separate to avoid merge drift)."""

import asyncio

from app.core.celery import celery_app
from app.core.email import mailer


@celery_app.task(name="app.tasks.tasks.send_signup_confirmation_email")
def send_signup_confirmation_email(to_email: str, confirmation_url: str) -> None:
    """Queue signup / email-verification message (HTML + plain text)."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            mailer.send_email(
                email=to_email,
                subject="Verify your email",
                email_template="signup_confirmation.html",
                text_content=f"Confirm your account:\n{confirmation_url}\n",
                template_vars={"ConfirmationURL": confirmation_url},
            )
        )
        loop.close()
    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.error(
            f"Failed to send signup confirmation to {to_email}: {e}",
            exc_info=True,
        )
        raise


@celery_app.task(name="app.tasks.tasks.send_password_reset_email")
def send_password_reset_email(to_email: str, reset_url: str) -> None:
    """Queue password reset message (HTML + plain text)."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            mailer.send_email(
                email=to_email,
                subject="Password reset",
                email_template="reset_password.html",
                text_content=f"Reset your password:\n{reset_url}\n",
                template_vars={"ConfirmationURL": reset_url},
            )
        )
        loop.close()
    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.error(
            f"Failed to send password reset email to {to_email}: {e}",
            exc_info=True,
        )
        raise
