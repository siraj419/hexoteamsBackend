from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.schemas.auth import (
    AuthLoginRequest, 
    AuthLoginResponse, 
    AuthRegisterRequest, 
    AuthRegisterResponse,
    AuthLogoutResponse,
    AuthRefreshTokenResponse,
    AuthConfirmRequest,
    AuthConfirmResponse,
    AuthForgetPasswordRequest,
    AuthForgetPasswordResponse,
    AuthResetPasswordRequest,
    AuthResetPasswordResponse,
    AuthChangePasswordRequest,
    AuthChangePasswordResponse,
    AuthUpdateProfileRequest,
    AuthUpdateProfileResponse,
    AuthChangeAvatarResponse,
    AuthRemoveAvatarResponse,
    User
)
from app.services.auth import AuthService
from app.services.files import FilesService
from app.routers.deps import get_current_user
from app.utils.redis_cache import UserMeCache
from sqlalchemy import select

from app.db.sync_session import SyncSessionLocal
from app.models import Profile
from pydantic import UUID4

router = APIRouter()

@router.post("/register", response_model=AuthRegisterResponse, status_code=status.HTTP_201_CREATED)
def register(auth_request: AuthRegisterRequest):
    """
        Register a new user and return the message
    """
    auth_service = AuthService()
    return auth_service.register(auth_request)

@router.post("/confirm", response_model=AuthConfirmResponse, status_code=status.HTTP_200_OK)
def confirm(auth_request: AuthConfirmRequest, response: Response):
    """"
        Confirm the user's email and set the session
    """
    auth_service = AuthService()
    return auth_service.confirm(auth_request, response)

@router.post("/refresh", response_model=AuthRefreshTokenResponse, status_code=status.HTTP_200_OK)
def refresh(request: Request, response: Response):
    """
        Refresh the access token
    """
    auth_service = AuthService()
    return auth_service.refresh(request, response)

@router.post("/login", response_model=AuthLoginResponse, status_code=status.HTTP_200_OK)
def login(auth_request: AuthLoginRequest, response: Response):
    """
        Login the user and return the access token and save the refresh token in the cookies
    """
    auth_service = AuthService()
    return auth_service.login(auth_request, response)

@router.post("/logout", response_model=AuthLogoutResponse, status_code=status.HTTP_200_OK)
def logout(response: Response):
    """
        Logout the user and delete the refresh token from the cookies
    """
    auth_service = AuthService()
    return auth_service.logout(response)

@router.post("/forget-password", response_model=AuthForgetPasswordResponse, status_code=status.HTTP_200_OK)
def forget_password(auth_request: AuthForgetPasswordRequest):
    """
        Forget the user's password and send a reset email
    """
    auth_service = AuthService()
    return auth_service.forget_password(auth_request)

@router.post("/reset-password", response_model=AuthResetPasswordResponse, status_code=status.HTTP_200_OK)
def reset_password(auth_request: AuthResetPasswordRequest, response: Response):
    """
        Update the user's password after they follow the recovery link
    """
    auth_service = AuthService()
    return auth_service.reset_password(auth_request, response)

@router.get("/me", response_model=User, status_code=status.HTTP_200_OK)
def get_me(user: any = Depends(get_current_user)):
    """
        Get the current user from profiles table
    """
    user_id_str = str(user.id)
    
    cached_user = UserMeCache.get_user(user_id_str)
    if cached_user:
        return User(**cached_user)
    
    try:
        db = SyncSessionLocal()
        try:
            profile = db.execute(
                select(Profile).where(Profile.user_id == user.id)
            ).scalar_one_or_none()
        finally:
            db.close()

        if not profile:
            display_name = (
                (user.user_metadata or {}).get("display_name")
                or (user.user_metadata or {}).get("name")
                or (user.user_metadata or {}).get("full_name")
                or (user.email.split("@")[0] if user.email else "User")
            )
            user_response = User(
                id=str(user.id),
                display_name=display_name,
                email=user.email,
                timezone="UTC",
                avatar_url=None,
                browser_notifications=True,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )
        else:
            avatar_url = None
            if profile.avatar_file_id:
                try:
                    files_service = FilesService()
                    avatar_url = files_service.get_file_url(UUID4(str(profile.avatar_file_id)))
                except Exception:
                    avatar_url = None

            user_response = User(
                id=str(user.id),
                display_name=profile.display_name or (user.email.split("@")[0] if user.email else "User"),
                email=profile.email,
                timezone=profile.timezone or "UTC",
                avatar_url=avatar_url,
                browser_notifications=profile.browser_notifications,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )

        UserMeCache.set_user(user_id_str, user_response.model_dump(mode="json"))
        return user_response

    except Exception:
        display_name = (
            (user.user_metadata or {}).get("display_name")
            or (user.user_metadata or {}).get("name")
            or (user.user_metadata or {}).get("full_name")
            or (user.email.split("@")[0] if user.email else "User")
        )
        return User(
            id=str(user.id),
            display_name=display_name,
            email=user.email,
            timezone="UTC",
            avatar_url=None,
            browser_notifications=True,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

@router.post("/change-password", response_model=AuthChangePasswordResponse, status_code=status.HTTP_200_OK)
def change_password(auth_request: AuthChangePasswordRequest, user: any = Depends(get_current_user)):
    """
        Change the user's password
    """
    auth_service = AuthService()
    return auth_service.change_password(auth_request, user)

@router.put("/update-profile", response_model=AuthUpdateProfileResponse, status_code=status.HTTP_200_OK)
def update_profile(auth_request: AuthUpdateProfileRequest, user: any = Depends(get_current_user)):
    """
        Update the user's profile settings
    """
    auth_service = AuthService()
    return auth_service.update_profile(auth_request, user)

@router.post("/change-avatar", response_model=AuthChangeAvatarResponse, status_code=status.HTTP_200_OK)
def change_avatar(file: UploadFile = File(...), user: any = Depends(get_current_user)):
    """
        Change the user's avatar
    """
    auth_service = AuthService()
    return auth_service.change_avatar(user, file)

@router.delete("/remove-avatar", response_model=AuthRemoveAvatarResponse, status_code=status.HTTP_200_OK)
def remove_avatar(user: any = Depends(get_current_user)):
    """
        Remove the user's avatar
    """
    auth_service = AuthService()
    return auth_service.remove_avatar(user)

