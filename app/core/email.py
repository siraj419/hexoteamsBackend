from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from fastapi import HTTPException, status
from pydantic import EmailStr
import aiosmtplib
import os
import asyncio


class Mailer:
    def __init__(self):
        from app.core.config import Settings
        self.settings = settings = Settings()
        self._client = None
        self._loop = None
    
    def _load_template(self, template_name: str):
        try:
            base_path = os.path.join(os.getcwd(), self.settings.EMAIL_TEMPLATES_PATH, template_name)
            with open(base_path, "r") as f:
                template = f.read()
                
        except FileNotFoundError as e:
            raise e
        except Exception as e:
            raise e
        return template
    
    async def _get_client(self):
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop != loop:
            self._client = aiosmtplib.SMTP(
                hostname=self.settings.SMTP_HOST,
                port=self.settings.SMTP_PORT,
                use_tls=self.settings.SMTP_USE_TLS,
                username=self.settings.SMTP_USERNAME,
                password=self.settings.SMTP_PASSWORD,
            )
            await self._client.connect()
            self._loop = loop
        return self._client

    async def send_email(
        self,
        email: EmailStr,
        subject: str,
        email_template: str = None,
        text_content: str | None = None,
        token: str | None = None,
        template_vars: dict | None = None,
    ):
        client = await self._get_client()

        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = f"{self.settings.FROM_NAME} <{self.settings.FROM_EMAIL}>"
        message["To"] = email

        if text_content:
            message.attach(MIMEText(text_content, "plain"))

        if email_template:
            html_content = self._load_template(str(email_template))
            
            # Replace token if provided (for backward compatibility)
            if token:
                html_content = html_content.replace("{{token}}", token)
            
            # Replace template variables if provided
            if template_vars:
                for key, value in template_vars.items():
                    html_content = html_content.replace(f"{{{{{key}}}}}", str(value))
            
            message.attach(MIMEText(html_content, "html"))

        try:
            if not client.is_connected:
                await client.connect()
            await client.send_message(message)
            print(f"Email sent to {email}")
        except Exception as e:
            raise e

    async def disconnect(self):
        if self._client:
            await self._client.quit()
            self._client = None
            self._loop = None
        
mailer = Mailer()