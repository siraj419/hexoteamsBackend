from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
TOKEN_TYPE_EMAIL_VERIFY = "email_verify"
TOKEN_TYPE_PASSWORD_RESET = "password_reset"


@dataclass
class AuthenticatedUser:
    """Drop-in replacement for Supabase user in route handlers."""

    id: UUID
    email: str
    user_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


def hash_password(plain_password: str) -> str:
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    return pwd_context.verify(plain_password, password_hash)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: UUID, email: str) -> tuple[str, int]:
    expires_delta = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = _utcnow() + expires_delta
    expires_in = int(expires_delta.total_seconds())
    payload = {
        "sub": str(user_id),
        "email": email,
        "type": TOKEN_TYPE_ACCESS,
        "exp": expire,
    }
    token = jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return token, expires_in


def create_refresh_token(user_id: UUID) -> tuple[str, int]:
    expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    expire = _utcnow() + expires_delta
    expires_in = int(expires_delta.total_seconds())
    payload = {
        "sub": str(user_id),
        "type": TOKEN_TYPE_REFRESH,
        "exp": expire,
    }
    token = jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return token, expires_in


def create_email_verification_token(user_id: UUID, email: str) -> str:
    expire = _utcnow() + timedelta(hours=settings.JWT_EMAIL_VERIFY_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "email": email,
        "type": TOKEN_TYPE_EMAIL_VERIFY,
        "exp": expire,
    }
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_password_reset_token(user_id: UUID, email: str) -> str:
    expire = _utcnow() + timedelta(minutes=settings.JWT_PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "email": email,
        "type": TOKEN_TYPE_PASSWORD_RESET,
        "exp": expire,
    }
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "type"]},
        )
    except JWTError as e:
        raise ValueError("Invalid or expired token") from e
    if expected_type is not None and payload.get("type") != expected_type:
        raise ValueError("Invalid token type")
    return payload
