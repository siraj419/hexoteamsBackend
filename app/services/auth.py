from fastapi import HTTPException, status, Request, Response, UploadFile
from pydantic import UUID4

from datetime import datetime, timezone
from supabase_auth.errors import AuthApiError
from pydantic import EmailStr
from typing import Any
import time
import httpx

from app.core import supabase, settings
from app.services.files import FilesService
from app.utils.redis_cache import UserMeCache
import logging

logger = logging.getLogger(__name__)
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

class AuthService:
    def __init__(self):
        self.files_service = FilesService()
    
    def register(self, auth_request: AuthRegisterRequest) -> AuthRegisterResponse:
        
        # check if the user exists
        response = self._check_user_exists_and_verified(auth_request.email)
        
        if response and hasattr(response, 'data') and response.data:
            if response.data[0].get('user_exists'):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "message": "User already exists",
                        "is_verified": response.data[0].get('verified')
                    }
                )
        
        # register the user
        try:
            supabase.auth.sign_up(
                {
                    "email": auth_request.email,
                    "password": auth_request.password,
                    "options": {
                        "data": {
                            "display_name": auth_request.display_name
                        },
                        "email_redirect_to": f"{settings.FRONTEND_URL}/email-verification"
                    }
                }
            )
            
        except AuthApiError as e:
            if 'you can only auth_request this after' in str(e):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Please wait before trying again"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to register user: {e}"
                )
        
        return AuthRegisterResponse(
            message="User registered successfully, check your email for verification"
        )
        
    def confirm(self, auth_request: AuthConfirmRequest, response: Response) -> AuthConfirmResponse:
        
        # get the user
        try:
            user_response = supabase.auth.get_user(auth_request.access_token)
        except AuthApiError as e:
            raise e
        
        if not user_response:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized, provide a valid token"
            )
        
        # save the refresh token in the cookies
        response.set_cookie(
            key="refresh_token",
            value=auth_request.refresh_token,
            httponly=True,
            secure=False,
        )
        
        # create the profile
        self._create_profile(user_response.user)
        
        return AuthConfirmResponse(
            access_token=auth_request.access_token,
            expires_in=auth_request.access_token_expires_in
        )
    
    def refresh(self, request: Request) -> AuthRefreshTokenResponse:
        # get the refresh token from the cookies
        refresh_token = request.cookies.get("refresh_token")
        if not refresh_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized, provide a valid token"
            )
        
        # validate the refresh token and get the new access token
        try:
            auth_response = supabase.auth.refresh_session(refresh_token)
        except AuthApiError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized, provide a valid token"
            )
        
        return AuthRefreshTokenResponse(
            access_token=auth_response.session.access_token,
            expires_in=auth_response.session.expires_in
        )

    
    def login(self, auth_request: AuthLoginRequest, response: Response) -> AuthLoginResponse:
        try:
            auth_response = supabase.auth.sign_in_with_password({
                "email": auth_request.email,
                "password": auth_request.password
            })

        except AuthApiError as e:
            if e.code == 'email_not_confirmed':
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "message": "Email not confirmed",
                        "is_verified": False
                    }
                )
            if 'you can only auth_request this after' in str(e):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Please wait before trying again"
                )
            
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "message": "Invalid credentials",
                    "is_verified": True
                }
            )
        
        # save the refresh token in the cookies
        response.set_cookie(
            key="refresh_token",
            value=auth_response.session.refresh_token,
            httponly=True,
            secure=False,
        )
        
        # save supabase session
        supabase.auth.set_session(auth_response.session.access_token, auth_response.session.refresh_token)
        
        return AuthLoginResponse(
            access_token=auth_response.session.access_token,
            expires_in=auth_response.session.expires_in
        )
        
    
    def logout(self, response: Response) -> AuthLogoutResponse:
        # delete the refresh token from the cookies
        response.delete_cookie(key="refresh_token")
        
        # sign out the user
        try:
            supabase.auth.sign_out()
        except AuthApiError as e:
            raise e

        return AuthLogoutResponse(
            message="User logged out successfully"
        )

    def forget_password(self, auth_request: AuthForgetPasswordRequest) -> AuthForgetPasswordResponse:
        try:
            options = {}
            # Use provided redirect_to or default to FRONTEND_URL/reset-password
            redirect_url = auth_request.redirect_to or f"{settings.FRONTEND_URL}/reset-password"
            options["redirect_to"] = redirect_url
                
            supabase.auth.reset_password_for_email(auth_request.email, options)
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )

        return AuthForgetPasswordResponse(
            message="Password reset email sent"
        )

    def reset_password(self, auth_request: AuthResetPasswordRequest, response: Response) -> AuthResetPasswordResponse:
        # set the session using the recovery tokens from the email link
        try:
            supabase.auth.set_session(auth_request.access_token, auth_request.refresh_token)
        except AuthApiError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired recovery token"
            )

        # update the password
        try:
            supabase.auth.update_user({"password": auth_request.password})
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=e.message
            )
            
        # fetch session
        session = supabase.auth.get_session()
        if not session:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not create session after password reset"
            )

        # save the refresh token in the cookies
        response.set_cookie(
            key="refresh_token",
            value=session.refresh_token,
            httponly=True,
            secure=False,
        )

        return AuthResetPasswordResponse(
            access_token=session.access_token,
            expires_in=session.expires_in
        )

    def change_password(self, auth_request: AuthChangePasswordRequest, user: any) -> AuthChangePasswordResponse:
        # Verify current password by attempting to sign in
        try:
            supabase.auth.sign_in_with_password({
                "email": user.email,
                "password": auth_request.current_password
            })
        except AuthApiError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect"
            )

        # Update the password
        try:
            supabase.auth.update_user({"password": auth_request.new_password})
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to update password: {str(e)}"
            )

        return AuthChangePasswordResponse(
            message="Password changed successfully"
        )

    def update_profile(self, auth_request: AuthUpdateProfileRequest, user: any) -> AuthUpdateProfileResponse:
        update_data = {}
        
        if auth_request.display_name is not None:
            update_data['display_name'] = auth_request.display_name
        
        if auth_request.timezone is not None:
            update_data['timezone'] = auth_request.timezone
        
        if auth_request.browser_notifications is not None:
            update_data['browser_notifications'] = auth_request.browser_notifications
        
        if auth_request.email_notifications is not None:
            update_data['email_notifications'] = auth_request.email_notifications
        
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        update_data['updated_at'] = datetime.now(timezone.utc).isoformat()
        
        try:
            response = supabase.table('profiles').update(update_data).eq('user_id', user.id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update profile: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Profile not found"
            )
        
        # Note: display_name is now stored in profiles table, not in user_metadata
        # So we don't need to update Supabase auth user_metadata anymore
        
        # Invalidate user cache since profile was updated
        UserMeCache.delete_user(str(user.id))
        
        return AuthUpdateProfileResponse(
            message="Profile updated successfully"
        )

    def change_avatar(self, user: any, file: UploadFile) -> AuthChangeAvatarResponse:
        if not self.files_service.validate_file_extension(file.filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file extension"
            )
        
        if not self.files_service.validate_file_size(file.size):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File size exceeds the maximum allowed size"
            )
        
        try:
            profile_response = supabase.table('profiles').select('avatar_file_id').eq('user_id', user.id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get profile: {str(e)}"
            )
        
        if not profile_response.data or len(profile_response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Profile not found"
            )
        
        avatar_file_id = profile_response.data[0].get('avatar_file_id')
        
        if avatar_file_id:
            try:
                file_data = self.files_service.update_file(UUID4(avatar_file_id), file)
                file_id = UUID4(file_data['id'])
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to update avatar file: {str(e)}"
                )
        else:
            try:
                file_data = self.files_service.upload_file(file, UUID4(user.id))
                file_id = file_data.id
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to upload avatar file: {str(e)}"
                )
        
        avatar_url = self.files_service.get_file_url(file_id)
        
        try:
            response = supabase.table('profiles').update({
                'avatar_file_id': str(file_id),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('user_id', user.id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update profile avatar: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Profile not found"
            )
        
        # Invalidate user cache since avatar was changed
        UserMeCache.delete_user(str(user.id))
        
        return AuthChangeAvatarResponse(avatar_url=avatar_url)

    def remove_avatar(self, user: any) -> AuthRemoveAvatarResponse:
        try:
            profile_response = supabase.table('profiles').select('avatar_file_id').eq('user_id', user.id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get profile: {str(e)}"
            )
        
        if not profile_response.data or len(profile_response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Profile not found"
            )
        
        avatar_file_id = profile_response.data[0].get('avatar_file_id')
        
        if not avatar_file_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No avatar to remove"
            )
        
        try:
            response = supabase.table('profiles').update({
                'avatar_file_id': None,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('user_id', user.id).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to remove avatar: {str(e)}"
            )
        
        if not response.data or len(response.data) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Profile not found"
            )
        
        try:
            self.files_service.delete_file_permanently(UUID4(avatar_file_id))
        except Exception as e:
            pass
        
        # Invalidate user cache since avatar was removed
        UserMeCache.delete_user(str(user.id))
        
        return AuthRemoveAvatarResponse(
            message="Avatar removed successfully"
        )
    
    def _create_profile(self, user: any) -> Any:
        max_retries = 3
        retry_delay = 0.5
        
        # check if the profile exists by user_id
        for attempt in range(max_retries):
            try:
                response = (
                    supabase.table('profiles')
                    .select('*')
                    .eq('user_id', user.id)
                    .execute()
                )
                break
            except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    logger.warning(f"Network error checking profile by user_id (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Failed to check profile by user_id after {max_retries} attempts: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Service temporarily unavailable. Please try again in a moment."
                    )
        
        if response.data and len(response.data) > 0:
            return response.data[0]
        
        # Also check by email to avoid duplicate key errors
        for attempt in range(max_retries):
            try:
                email_check = (
                    supabase.table('profiles')
                    .select('*')
                    .eq('email', user.email)
                    .execute()
                )
                break
            except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    logger.warning(f"Network error checking profile by email (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Failed to check profile by email after {max_retries} attempts: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Service temporarily unavailable. Please try again in a moment."
                    )
        
        if email_check.data and len(email_check.data) > 0:
            # Profile exists with this email
            existing_profile = email_check.data[0]
            # If user_id is different, update it
            if existing_profile.get('user_id') != user.id:
                for attempt in range(max_retries):
                    try:
                        update_response = (
                            supabase.table('profiles')
                            .update({
                                'user_id': user.id,
                                'updated_at': datetime.now(timezone.utc).isoformat(),
                            })
                            .eq('email', user.email)
                            .execute()
                        )
                        if update_response.data and len(update_response.data) > 0:
                            return update_response.data[0]
                        break
                    except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)
                            logger.warning(f"Network error updating profile (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                            time.sleep(wait_time)
                            continue
                        else:
                            logger.warning(f"Failed to update profile for email {user.email} after {max_retries} attempts: {e}")
                    except Exception as e:
                        logger.warning(f"Failed to update profile for email {user.email}: {e}")
                        break
            return existing_profile
        
        # Get display_name from user_metadata, fallback to name, full_name, or email
        display_name = (
            user.user_metadata.get('display_name') or
            user.user_metadata.get('name') or
            user.user_metadata.get('full_name') or
            user.email.split('@')[0] if user.email else 'User'
        )
        
        # create profile with retry logic
        for attempt in range(max_retries):
            try:
                response = supabase.table('profiles').insert({
                    'user_id': user.id,
                    'email': user.email,
                    'display_name': display_name,
                    'timezone': 'UTC',
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                }).execute()
                
                if response.data and len(response.data) > 0:
                    return response.data[0]
                return response.data
            except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    logger.warning(f"Network error creating profile (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Failed to create profile after {max_retries} attempts: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Service temporarily unavailable. Please try again in a moment."
                    )
            except Exception as e:
                # If insert fails due to duplicate key (race condition), fetch existing profile
                error_str = str(e)
                if 'duplicate key' in error_str.lower() or '23505' in error_str:
                    logger.warning(f"Profile insert failed due to duplicate key for user {user.id}, email {user.email}. Fetching existing profile.")
                    for fetch_attempt in range(max_retries):
                        try:
                            existing_profile = (
                                supabase.table('profiles')
                                .select('*')
                                .eq('email', user.email)
                                .execute()
                            )
                            if existing_profile.data and len(existing_profile.data) > 0:
                                return existing_profile.data[0]
                            break
                        except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as fetch_e:
                            if fetch_attempt < max_retries - 1:
                                wait_time = retry_delay * (2 ** fetch_attempt)
                                logger.warning(f"Network error fetching existing profile (attempt {fetch_attempt + 1}/{max_retries}): {fetch_e}. Retrying in {wait_time}s...")
                                time.sleep(wait_time)
                                continue
                            else:
                                logger.error(f"Failed to fetch existing profile after {max_retries} attempts: {fetch_e}")
                                break
                # If it's not a duplicate key error, re-raise
                logger.error(f"Failed to create profile for user {user.id}, email {user.email}: {e}")
                raise
    
    def _check_user_exists_and_verified(self, email: EmailStr) -> dict:
        
        try:
            # Check if user exists in profiles table (which links to auth.users)
            # This doesn't require authentication and works with service role key
            response = supabase.rpc('check_user_exists_and_verified', {'email_input': email.lower()}).execute()
            return response
            
        except Exception as e:
            # If checking fails, log and return False to allow signup attempt
            # Supabase signup will handle duplicate check anyway
            logger.warning(f"Failed to check if user exists for email {email}: {e}")
            return False