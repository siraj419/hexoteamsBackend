import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, Response, UploadFile, status
from pydantic import UUID4, EmailStr
from app.utils.uuid_compat import as_uuid
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core import settings
from app.core.security import (
    TOKEN_TYPE_EMAIL_VERIFY,
    TOKEN_TYPE_PASSWORD_RESET,
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.sync_session import SyncSessionLocal
from app.models import Profile
from app.schemas.auth import (
    AuthChangeAvatarResponse,
    AuthChangePasswordRequest,
    AuthChangePasswordResponse,
    AuthConfirmRequest,
    AuthConfirmResponse,
    AuthForgetPasswordRequest,
    AuthForgetPasswordResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthLogoutResponse,
    AuthRefreshTokenResponse,
    AuthRegisterRequest,
    AuthRegisterResponse,
    AuthRemoveAvatarResponse,
    AuthResetPasswordRequest,
    AuthResetPasswordResponse,
    AuthUpdateProfileRequest,
    AuthUpdateProfileResponse,
)
from app.services.files import FilesService
from app.services.time_log import TimeLogService
from app.tasks.tasks import send_password_reset_email, send_signup_confirmation_email
from app.utils.redis_cache import UserMeCache

logger = logging.getLogger(__name__)


def _issue_session(response: Response, user_id: UUID, email: str) -> tuple[str, int]:
    access_token, expires_in = create_access_token(user_id, email)
    refresh_token, _ = create_refresh_token(user_id)
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=False,
    )
    return access_token, expires_in


class AuthService:
    def __init__(self):
        self.files_service = FilesService()

    def register(self, auth_request: AuthRegisterRequest) -> AuthRegisterResponse:
        email_norm = auth_request.email.lower()
        db = SyncSessionLocal()
        try:
            existing = db.execute(
                select(Profile).where(func.lower(Profile.email) == email_norm)
            ).scalar_one_or_none()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "message": "User already exists",
                        "is_verified": bool(existing.email_verified),
                    },
                )

            user_id = uuid4()
            profile = Profile(
                user_id=user_id,
                email=email_norm,
                password_hash=hash_password(auth_request.password),
                display_name=auth_request.display_name,
                email_verified=False,
            )
            db.add(profile)
            db.commit()
            db.refresh(profile)

            verify_jwt = create_email_verification_token(user_id, email_norm)
            verify_url = (
                f"{settings.FRONTEND_URL.rstrip('/')}/email-verification?"
                f"token={urllib.parse.quote(verify_jwt)}"
            )
            send_signup_confirmation_email.delay(str(email_norm), verify_url)
        except HTTPException:
            db.rollback()
            raise
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "User already exists", "is_verified": True},
            )
        finally:
            db.close()

        return AuthRegisterResponse(
            message="User registered successfully, check your email for verification"
        )

    def confirm(self, auth_request: AuthConfirmRequest, response: Response) -> AuthConfirmResponse:
        try:
            payload = decode_token(auth_request.access_token, expected_type=TOKEN_TYPE_EMAIL_VERIFY)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired verification token",
            )

        user_id = UUID(payload["sub"])
        db = SyncSessionLocal()
        access_token = ""
        expires_in = 0
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == user_id)
            ).scalar_one_or_none()
            if not profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found",
                )
            profile.email_verified = True
            profile.updated_at = datetime.now(timezone.utc)
            db.commit()

            access_token, expires_in = _issue_session(response, profile.user_id, profile.email)
        finally:
            db.close()

        return AuthConfirmResponse(
            access_token=access_token,
            expires_in=expires_in,
        )

    def refresh(self, request: Request, response: Response) -> AuthRefreshTokenResponse:
        refresh_token = request.cookies.get("refresh_token")
        if not refresh_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized, provide a valid token",
            )
        try:
            payload = decode_token(refresh_token, expected_type=TOKEN_TYPE_REFRESH)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized, provide a valid token",
            )

        user_id = UUID(payload["sub"])
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == user_id)
            ).scalar_one_or_none()
            if not profile:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unauthorized, provide a valid token",
                )
            access_token, expires_in = create_access_token(profile.user_id, profile.email)
            new_refresh, _ = create_refresh_token(profile.user_id)
        finally:
            db.close()

        response.set_cookie(
            key="refresh_token",
            value=new_refresh,
            httponly=True,
            secure=False,
        )
        return AuthRefreshTokenResponse(access_token=access_token, expires_in=expires_in)

    def login(self, auth_request: AuthLoginRequest, response: Response) -> AuthLoginResponse:
        email_norm = auth_request.email.lower()
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(func.lower(Profile.email) == email_norm)
            ).scalar_one_or_none()
            if not profile or not verify_password(auth_request.password, profile.password_hash):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "message": "Invalid credentials",
                        "is_verified": True,
                    },
                )
            if not profile.email_verified:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "message": "Email not confirmed",
                        "is_verified": False,
                    },
                )

            access_token, expires_in = _issue_session(response, profile.user_id, profile.email)
        finally:
            db.close()

        return AuthLoginResponse(access_token=access_token, expires_in=expires_in)

    def logout(self, response: Response) -> AuthLogoutResponse:
        response.delete_cookie(key="refresh_token")
        return AuthLogoutResponse(message="User logged out successfully")

    def forget_password(self, auth_request: AuthForgetPasswordRequest) -> AuthForgetPasswordResponse:
        email_norm = auth_request.email.lower()
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(func.lower(Profile.email) == email_norm)
            ).scalar_one_or_none()
            if profile and profile.password_hash:
                reset_jwt = create_password_reset_token(profile.user_id, profile.email)
                base = settings.FRONTEND_URL.rstrip("/")
                reset_url = f"{base}/reset-password?token={urllib.parse.quote(reset_jwt)}"
                send_password_reset_email.delay(str(email_norm), reset_url)
        finally:
            db.close()

        return AuthForgetPasswordResponse(message="Password reset email sent")

    def reset_password(self, auth_request: AuthResetPasswordRequest, response: Response) -> AuthResetPasswordResponse:
        try:
            payload = decode_token(auth_request.access_token, expected_type=TOKEN_TYPE_PASSWORD_RESET)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired recovery token",
            )

        user_id = UUID(payload["sub"])
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == user_id)
            ).scalar_one_or_none()
            if not profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found",
                )
            profile.password_hash = hash_password(auth_request.password)
            profile.updated_at = datetime.now(timezone.utc)
            db.commit()

            access_token, expires_in = _issue_session(response, profile.user_id, profile.email)
        finally:
            db.close()

        return AuthResetPasswordResponse(access_token=access_token, expires_in=expires_in)

    def change_password(self, auth_request: AuthChangePasswordRequest, user: Any) -> AuthChangePasswordResponse:
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == user.id)
            ).scalar_one_or_none()
            if not profile or not verify_password(auth_request.current_password, profile.password_hash):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Current password is incorrect",
                )
            profile.password_hash = hash_password(auth_request.new_password)
            profile.updated_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

        return AuthChangePasswordResponse(message="Password changed successfully")

    def update_profile(
        self, auth_request: AuthUpdateProfileRequest, user: Any
    ) -> AuthUpdateProfileResponse:
        update_data: dict[str, Any] = {}
        if auth_request.display_name is not None:
            update_data["display_name"] = auth_request.display_name
        if auth_request.timezone is not None:
            update_data["timezone"] = auth_request.timezone
        if auth_request.browser_notifications is not None:
            update_data["browser_notifications"] = auth_request.browser_notifications
        if auth_request.email_notifications is not None:
            update_data["email_notifications"] = auth_request.email_notifications

        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update",
            )

        update_data["updated_at"] = datetime.now(timezone.utc)
        db = SyncSessionLocal()
        try:
            result = db.execute(
                update(Profile).where(Profile.user_id == user.id).values(**update_data)
            )
            db.commit()
            if result.rowcount == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found",
                )
        finally:
            db.close()

        UserMeCache.delete_user(str(user.id))
        if "timezone" in update_data:
            TimeLogService().invalidate_user_timezone_caches(as_uuid(str(user.id)))

        return AuthUpdateProfileResponse(message="Profile updated successfully")

    def change_avatar(self, user: Any, file: UploadFile) -> AuthChangeAvatarResponse:
        if not self.files_service.validate_file_extension(file.filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file extension",
            )
        if not self.files_service.validate_file_size(file.size):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File size exceeds the maximum allowed size",
            )

        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == user.id)
            ).scalar_one_or_none()
            if not profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found",
                )

            avatar_file_id = profile.avatar_file_id
            if avatar_file_id:
                try:
                    file_data = self.files_service.update_file(as_uuid(str(avatar_file_id)), file)
                    file_id = as_uuid(file_data["id"])
                except Exception as e:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to update avatar file: {str(e)}",
                    )
            else:
                try:
                    file_data = self.files_service.upload_file(file, as_uuid(str(user.id)))
                    file_id = file_data.id
                except Exception as e:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to upload avatar file: {str(e)}",
                    )

            profile.avatar_file_id = file_id
            profile.updated_at = datetime.now(timezone.utc)
            db.commit()
            avatar_url = self.files_service.get_file_url(file_id)
        finally:
            db.close()

        UserMeCache.delete_user(str(user.id))
        return AuthChangeAvatarResponse(avatar_url=avatar_url)

    def remove_avatar(self, user: Any) -> AuthRemoveAvatarResponse:
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == user.id)
            ).scalar_one_or_none()
            if not profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found",
                )
            if not profile.avatar_file_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No avatar to remove",
                )
            avatar_file_id = profile.avatar_file_id
            profile.avatar_file_id = None
            profile.updated_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

        try:
            self.files_service.delete_file_permanently(as_uuid(str(avatar_file_id)))
        except Exception:
            pass

        UserMeCache.delete_user(str(user.id))
        return AuthRemoveAvatarResponse(message="Avatar removed successfully")
