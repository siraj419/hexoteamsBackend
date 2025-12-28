from fastapi import APIRouter, status, Depends, Request, Response, File, UploadFile, Query

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
    User,
    OAuthInitiateResponse
)
from app.services.auth import AuthService
from app.services.files import FilesService
from app.routers.deps import get_current_user
from app.utils.redis_cache import UserMeCache
from app.core import supabase
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
def refresh(request: Request):
    """
        Refresh the access token
    """
    auth_service = AuthService()
    return auth_service.refresh(request)

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
    
    # Fetch user profile from profiles table
    try:
        profile_response = supabase.table('profiles').select(
            'display_name, email, timezone, avatar_file_id, browser_notifications'
        ).eq('user_id', user_id_str).execute()
        
        if not profile_response.data or len(profile_response.data) == 0:
            # Fallback to user data if profile doesn't exist
            display_name = (
                user.user_metadata.get('display_name') or
                user.user_metadata.get('name') or
                user.user_metadata.get('full_name') or
                user.email.split('@')[0] if user.email else 'User'
            )
            
            user_response = User(
                id=user.id,
                display_name=display_name,
                email=user.email,
                timezone='UTC',
                avatar_url=None,
                browser_notifications=True,
                created_at=user.created_at,
                updated_at=user.updated_at
            )
        else:
            profile = profile_response.data[0]
            
            # Get avatar URL if avatar_file_id exists
            avatar_url = None
            if profile.get('avatar_file_id'):
                try:
                    files_service = FilesService()
                    avatar_url = files_service.get_file_url(UUID4(profile['avatar_file_id']))
                except Exception:
                    # If avatar file doesn't exist or can't be accessed, set to None
                    avatar_url = None
            
            user_response = User(
                id=user.id,
                display_name=profile['display_name'],
                email=profile['email'],
                timezone=profile.get('timezone', 'UTC'),
                avatar_url=avatar_url,
                browser_notifications=profile.get('browser_notifications', True),
                created_at=user.created_at,
                updated_at=user.updated_at
            )
        
        UserMeCache.set_user(user_id_str, user_response.model_dump(mode='json'))
        
        return user_response
        
    except Exception as e:
        # Fallback to user data if profile fetch fails
        display_name = (
            user.user_metadata.get('display_name') or
            user.user_metadata.get('name') or
            user.user_metadata.get('full_name') or
            user.email.split('@')[0] if user.email else 'User'
        )
        
        user_response = User(
            id=user.id,
            display_name=display_name,
            email=user.email,
            timezone='UTC',
            avatar_url=None,
            browser_notifications=True,
            created_at=user.created_at,
            updated_at=user.updated_at
        )
        
        return user_response

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

@router.get("/oauth/google", response_model=OAuthInitiateResponse, status_code=status.HTTP_200_OK)
def oauth_google():
    """
        Initiate Google OAuth flow and return redirect URL
    """
    auth_service = AuthService()
    return auth_service.initiate_oauth("google")

@router.get("/oauth/github", response_model=OAuthInitiateResponse, status_code=status.HTTP_200_OK)
def oauth_github():
    """
        Initiate GitHub OAuth flow and return redirect URL
    """
    auth_service = AuthService()
    return auth_service.initiate_oauth("github")

@router.get("/oauth/callback/google")
async def oauth_google_callback(
    code: str = Query(...),
    state: str = Query(...),
    response: Response = None
):
    """
        OAuth callback endpoint for Google
        Handles the redirect from Google after user authentication
    """
    auth_service = AuthService()
    return await auth_service.handle_oauth_callback(code, state, "google", response)

@router.get("/oauth/callback/github")
async def oauth_github_callback(
    code: str = Query(...),
    state: str = Query(...),
    response: Response = None
):
    """
        OAuth callback endpoint for GitHub
        Handles the redirect from GitHub after user authentication
    """
    auth_service = AuthService()
    return await auth_service.handle_oauth_callback(code, state, "github", response)


