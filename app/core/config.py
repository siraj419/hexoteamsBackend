from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional, List

class Settings(BaseSettings):
    # App Details
    APP_NAME: str = "HexoTeams API"
    APP_VERSION: str = "1.0.0"
    APP_PORT: int = 8002
    
    # Supabase Details
    SUPABASE_URL: str
    SUPABASE_KEY: str
    
    # Frontend URL (for email redirections)
    FRONTEND_URL: str = Field('http://localhost:3000', description="Frontend application URL used in email redirections")
    
    # Supabase Admin (for creating users)
    SUPABASE_SERVICE_ROLE_KEY: Optional[str] = Field(default=None, description="Supabase Service Role Key for admin operations")
    
    
    # AWS Credentials
    AWS_ACCESS_KEY_ID: str = Field(..., description="AWS Access Key ID")
    AWS_SECRET_ACCESS_KEY: str = Field(..., description="AWS Secret Access Key")
    AWS_REGION: str = Field(default="us-east-1", description="AWS Region")
    
    # S3 Configuration
    S3_BUCKET_NAME: str = Field(..., description="Default S3 bucket name")
    S3_ENDPOINT_URL: Optional[str] = Field(default=None, description="Custom S3 endpoint URL")
    S3_USE_SSL: bool = Field(default=True, description="Use SSL for S3 connections")
    S3_SIGNATURE_VERSION: str = Field(default="s3v4", description="S3 signature version")
    
    # File Upload Settings
    S3_MAX_FILE_SIZE_MB: int = Field(default=100, description="Maximum file size in MB")
    S3_PUBLIC_READ: bool = Field(default=False, description="Make uploaded files publicly readable")
    S3_ALLOWED_EXTENSIONS_LIST: List[str] = Field(default=["jpg", "jpeg", "png", "gif", "bmp", "tiff", "ico", "webp", "svg", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"], description="Allowed file extensions")
    
    # Email Settings
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USERNAME: str
    SMTP_PASSWORD: str
    SMTP_USE_TLS: bool
    SMTP_USE_STARTTLS: bool
    FROM_EMAIL: str
    FROM_NAME: str
    
    # Email Templates Path
    EMAIL_TEMPLATES_PATH: str = "app/templates"
    
    # redis 
    REDIS_URL: str = 'redis://localhost:6379/0'
    
    # Presigned URL Settings
    S3_PRESIGNED_URL_EXPIRATION: int = Field(
        default=3600, 
        description="Presigned URL expiration time in seconds (default: 1 hour)"
    )
    
    # Pagination Settings
    DEFAULT_PAGINATION_LIMIT: int = Field(default=10, description="Default pagination limit")
    DEFAULT_PAGINATION_OFFSET: int = Field(default=0, description="Default pagination offset")
    
    # Invitation Settings
    INVITATION_TOKEN_EXPIRATION_HOURS: int = Field(default=24, description="Invitation token expiration time in hours (default: 24 hours)")
    
    # other details
    MAX_COMMENT_REPLY_DEPTH: int = Field(default=3, description="Maximum comment reply depth")
    MAX_SUBTASK_DEPTH: int = Field(default=3, description="Maximum subtask depth")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"