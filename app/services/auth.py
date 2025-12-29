from fastapi import HTTPException, status, Request, Response, UploadFile
from pydantic import UUID4

from datetime import datetime, timezone
from supabase_auth.errors import AuthApiError
from pydantic import EmailStr
from typing import Any

from app.core import supabase, settings
from app.services.files import FilesService
from app.utils.redis_cache import UserMeCache
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
        if self._check_user_exists(auth_request.email):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User already exists"
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
                        }
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
            
            if 'you can only auth_request this after' in str(e):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Please wait before trying again"
                )
            
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials"
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
            if auth_request.redirect_to:
                options["redirect_to"] = auth_request.redirect_to
                
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
        # check if the profile exists
        response = (
            supabase.table('profiles')
            .select('*')
            .eq('user_id', user.id)
            .execute()
        )
        
        if response.data:
            return
        
        # Get display_name from user_metadata, fallback to name, full_name, or email
        display_name = (
            user.user_metadata.get('display_name') or
            user.user_metadata.get('name') or
            user.user_metadata.get('full_name') or
            user.email.split('@')[0] if user.email else 'User'
        )
        
        # create profile
        response = supabase.table('profiles').insert({
            'user_id': user.id,
            'email': user.email,
            'display_name': display_name,
            'timezone': 'UTC',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }).execute()
        
        return response.data
    
    def _check_user_exists(self, email: EmailStr) -> bool:
        
        try:
            response = supabase.rpc("check_email_exists", {"email_input": email}).execute()
        except AuthApiError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check if user exists: {e}"
            )

        return response.data