from fastapi import HTTPException, status, Request, Response, UploadFile
from pydantic import UUID4

from datetime import datetime, timezone
from supabase_auth.errors import AuthApiError
from pydantic import EmailStr
from typing import Any
import secrets
import string
import httpx

from app.core import supabase, settings
from app.services.files import FilesService
from app.services.oauth import OAuthService
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

class AuthService:
    def __init__(self):
        self.files_service = FilesService()
        self.oauth_service = OAuthService()
    
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
        
        if auth_request.display_name is not None:
            try:
                supabase.auth.update_user({
                    "data": {
                        "display_name": auth_request.display_name
                    }
                })
            except AuthApiError as e:
                pass
        
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
    
    def initiate_oauth(self, provider: str) -> OAuthInitiateResponse:
        """
        Initiate OAuth flow for Google or GitHub
        Returns the OAuth URL to redirect the user to
        """
        if provider not in ["google", "github"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OAuth provider. Supported providers: google, github"
            )
        
        try:
            if provider == "google":
                auth_url = self.oauth_service.get_google_auth_url()
            else:
                auth_url = self.oauth_service.get_github_auth_url()
            
            return OAuthInitiateResponse(url=auth_url)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to initiate OAuth: {str(e)}"
            )
    
    async def handle_oauth_callback(self, code: str, state: str, provider: str, response: Response):
        """
        Handle OAuth callback from provider
        Exchanges code for user info, creates/updates user in Supabase, and redirects to frontend
        """
        if provider not in ["google", "github"]:
            from fastapi.responses import RedirectResponse
            error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error=invalid_provider"
            return RedirectResponse(url=error_url)
        
        try:
            # Get code_verifier and verify provider from state
            code_verifier, state_provider = self.oauth_service.get_code_verifier_from_state(state)
            if not code_verifier:
                from fastapi.responses import RedirectResponse
                error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error=invalid_state"
                return RedirectResponse(url=error_url)
            
            print(f"state_provider: {state_provider}")
            print(f"provider: {provider}")
            
            # Verify provider matches state
            if state_provider and state_provider != provider:
                from fastapi.responses import RedirectResponse
                error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error=provider_mismatch"
                return RedirectResponse(url=error_url)
            
            print(f"code: {code}")
            print(f"code_verifier: {code_verifier}")
            # Exchange code for user info from OAuth provider
            if provider == "google":
                oauth_user_data = await self.oauth_service.exchange_google_code(code, code_verifier)
            else:
                oauth_user_data = await self.oauth_service.exchange_github_code(code, code_verifier)
            
            email = oauth_user_data.get("email")
            if not email:
                from fastapi.responses import RedirectResponse
                error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error=no_email"
                return RedirectResponse(url=error_url)
            
            # Check if user exists in Supabase
            user_exists = self._check_user_exists(email)
            
            if user_exists:
                # User exists, sign them in
                try:
                    auth_response = await self._sign_in_oauth_user(email, oauth_user_data)
                except Exception as e:
                    from fastapi.responses import RedirectResponse
                    error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error=signin_failed"
                    return RedirectResponse(url=error_url)
            else:
                # Create new user in Supabase
                auth_response = await self._create_oauth_user(oauth_user_data)
            
            if not auth_response or not auth_response.session:
                from fastapi.responses import RedirectResponse
                error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error=session_failed"
                return RedirectResponse(url=error_url)
            
            user = auth_response.user
            
            # Create profile if it doesn't exist
            self._create_profile(user)
            
            # Save the refresh token in cookies
            response.set_cookie(
                key="refresh_token",
                value=auth_response.session.refresh_token,
                httponly=True,
                secure=False,
            )
            
            # Save supabase session
            supabase.auth.set_session(
                auth_response.session.access_token,
                auth_response.session.refresh_token
            )
            
            # Redirect to frontend with access token in URL
            from fastapi.responses import RedirectResponse
            success_url = (
                f"{settings.OAUTH_FRONTEND_SUCCESS_URL}"
                f"?access_token={auth_response.session.access_token}"
                f"&expires_in={auth_response.session.expires_in}"
                f"&token_type=Bearer"
            )
            return RedirectResponse(url=success_url)
        except HTTPException as e:
            print(f"Error: {e}")
            from fastapi.responses import RedirectResponse
            error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error={str(e.detail)}"
            return RedirectResponse(url=error_url)
        except Exception as e:
            from fastapi.responses import RedirectResponse
            error_url = f"{settings.OAUTH_FRONTEND_ERROR_URL}?error=unknown"
            return RedirectResponse(url=error_url)
    
    def _generate_random_password(self) -> str:
        """Generate a random password for OAuth users"""
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return ''.join(secrets.choice(alphabet) for i in range(32))
    
    async def _create_oauth_user(self, oauth_user_data: dict):
        """Create a new user in Supabase from OAuth data"""
        email = oauth_user_data.get("email")
        name = oauth_user_data.get("name", "")
        provider = oauth_user_data.get("provider")
        provider_id = oauth_user_data.get("provider_id")
        
        # Generate a random password (OAuth users won't use it for login)
        password = self._generate_random_password()
        
        # Use Admin API if available for better control
        if settings.SUPABASE_SERVICE_ROLE_KEY:
            try:
                async with httpx.AsyncClient() as client:
                    # Create user using Admin API (auto-confirmed)
                    create_response = await client.post(
                        f"{settings.SUPABASE_URL}/auth/v1/admin/users",
                        headers={
                            "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "email": email,
                            "password": password,
                            "email_confirm": True,
                            "user_metadata": {
                                "display_name": name,
                                "provider": provider,
                                "provider_id": provider_id,
                                "avatar_url": oauth_user_data.get("picture")
                            }
                        }
                    )
                    
                    if create_response.status_code in [200, 201]:
                        # User created, now sign them in
                        auth_response = supabase.auth.sign_in_with_password({
                            "email": email,
                            "password": password
                        })
                        return auth_response
                    elif create_response.status_code == 422:
                        # User might already exist, try to sign in
                        return await self._sign_in_oauth_user(email, oauth_user_data)
                    else:
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Failed to create user: {create_response.text}"
                        )
            except httpx.HTTPError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to create user via admin API: {str(e)}"
                )
        else:
            # Fallback to regular sign_up
            try:
                auth_response = supabase.auth.sign_up({
                    "email": email,
                    "password": password,
                    "options": {
                        "data": {
                            "display_name": name,
                            "provider": provider,
                            "provider_id": provider_id,
                            "avatar_url": oauth_user_data.get("picture")
                        }
                    }
                })
                
                # Try to sign in (might fail if email confirmation required)
                try:
                    auth_response = supabase.auth.sign_in_with_password({
                        "email": email,
                        "password": password
                    })
                    return auth_response
                except AuthApiError:
                    # Email confirmation required - this is a problem for OAuth flow
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Email confirmation required. Please set SUPABASE_SERVICE_ROLE_KEY for OAuth users."
                    )
            except AuthApiError as e:
                # If user already exists (race condition), try to sign in
                if "already registered" in str(e).lower() or "already exists" in str(e).lower():
                    return await self._sign_in_oauth_user(email, oauth_user_data)
                raise
    
    async def _sign_in_oauth_user(self, email: str, oauth_user_data: dict):
        """Sign in an existing OAuth user"""
        # For existing OAuth users, we need to use admin API to reset password
        # or use a different approach
        # Since we can't know their password, we'll use admin API to create a session
        if settings.SUPABASE_SERVICE_ROLE_KEY:
            return await self._create_session_with_admin(email, oauth_user_data)
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cannot sign in OAuth user without service role key. Please set SUPABASE_SERVICE_ROLE_KEY."
            )
    
    async def _confirm_user_with_admin(self, email: str):
        """Confirm user email using Supabase Admin API"""
        if not settings.SUPABASE_SERVICE_ROLE_KEY:
            return
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.SUPABASE_URL}/auth/v1/admin/users",
                headers={
                    "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "email": email,
                    "email_confirm": True
                }
            )
            # Don't raise on error, just log it
    
    async def _create_session_with_admin(self, email: str, oauth_user_data: dict):
        """Create a session for user using Supabase Admin API"""
        if not settings.SUPABASE_SERVICE_ROLE_KEY:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Service role key not configured"
            )
        
        async with httpx.AsyncClient() as client:
            # First, get user by email
            response = await client.get(
                f"{settings.SUPABASE_URL}/auth/v1/admin/users",
                headers={
                    "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}"
                },
                params={"email": email}
            )
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found"
                )
            
            users = response.json().get("users", [])
            if not users:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found"
                )
            
            user_id = users[0].get("id")
            
            # Generate a token for the user
            token_response = await client.post(
                f"{settings.SUPABASE_URL}/auth/v1/admin/users/{user_id}/generate_link",
                headers={
                    "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json"
                },
                json={"type": "magiclink"}
            )
            
            if token_response.status_code != 200:
                # Fallback: create a password reset and use that
                password = self._generate_random_password()
                await client.put(
                    f"{settings.SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                    headers={
                        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={"password": password}
                )
                
                # Now sign in with the new password
                auth_response = supabase.auth.sign_in_with_password({
                    "email": email,
                    "password": password
                })
                return auth_response
            
            # If we got a magic link, we can't use it directly
            # So we'll use the password reset approach above
            password = self._generate_random_password()
            await client.put(
                f"{settings.SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                headers={
                    "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json"
                },
                json={"password": password}
            )
            
            auth_response = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
            return auth_response