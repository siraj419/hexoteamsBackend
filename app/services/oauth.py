import httpx
import secrets
import hashlib
import base64
import json
from urllib.parse import urlencode
from fastapi import HTTPException, status
from typing import Dict, Any, Optional, Tuple

from app.core import settings
from app.utils.redis_cache import redis_client


class OAuthService:
    """Manual OAuth implementation for Google and GitHub"""
    
    def __init__(self):
        self.google_auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        self.google_token_url = "https://oauth2.googleapis.com/token"
        self.google_user_info_url = "https://www.googleapis.com/oauth2/v3/userinfo"
        
        self.github_auth_url = "https://github.com/login/oauth/authorize"
        self.github_token_url = "https://github.com/login/oauth/access_token"
        self.github_user_info_url = "https://api.github.com/user"
    
    def _generate_code_verifier(self) -> str:
        """Generate a code verifier for PKCE"""
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
    
    def _generate_code_challenge(self, verifier: str) -> str:
        """Generate a code challenge from verifier"""
        challenge = hashlib.sha256(verifier.encode('utf-8')).digest()
        return base64.urlsafe_b64encode(challenge).decode('utf-8').rstrip('=')
    
    def get_google_auth_url(self, state: Optional[str] = None) -> str:
        """
        Generate Google OAuth authorization URL
        Stores code_verifier in Redis with state as key
        """
        code_verifier = self._generate_code_verifier()
        code_challenge = self._generate_code_challenge(code_verifier)
        
        # Generate state if not provided
        if not state:
            state = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode('utf-8').rstrip('=')
        
        # Store code_verifier and provider in Redis with 10 minute TTL
        if redis_client:
            try:
                state_data = json.dumps({"code_verifier": code_verifier, "provider": "google"})
                redis_client.setex(f"oauth:state:{state}", 600, state_data)
            except Exception:
                pass  # Continue even if Redis fails
        
        # Use provider-specific callback URL
        redirect_uri = f"{settings.OAUTH_REDIRECT_URL}/google"
        
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            # "code_challenge": code_challenge,
            # "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "state": state
        }
        
        auth_url = f"{self.google_auth_url}?{urlencode(params)}"
        return auth_url
    
    def get_github_auth_url(self, state: Optional[str] = None) -> str:
        """
        Generate GitHub OAuth authorization URL
        Stores code_verifier in Redis with state as key
        """
        code_verifier = self._generate_code_verifier()
        code_challenge = self._generate_code_challenge(code_verifier)
        
        # Generate state if not provided
        if not state:
            state = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode('utf-8').rstrip('=')
        
        # Store code_verifier and provider in Redis with 10 minute TTL
        if redis_client:
            try:
                state_data = json.dumps({"code_verifier": code_verifier, "provider": "github"})
                redis_client.setex(f"oauth:state:{state}", 600, state_data)
            except Exception:
                pass  # Continue even if Redis fails
        
        # Use provider-specific callback URL
        redirect_uri = f"{settings.OAUTH_REDIRECT_URL}/github"
        
        params = {
            "client_id": settings.GITHUB_CLIENT_ID,
            # "redirect_uri": redirect_uri,
            "scope": "user:email",
            # "code_challenge": code_challenge,
            # "code_challenge_method": "S256",
            "state": state
        }
        
        auth_url = f"{self.github_auth_url}?{urlencode(params)}"
        return auth_url
    
    def get_code_verifier_from_state(self, state: str) -> Tuple[Optional[str], Optional[str]]:
        """Retrieve code_verifier and provider from Redis using state"""
        if not redis_client:
            return None, None
        try:
            state_data = redis_client.get(f"oauth:state:{state}")
            if state_data:
                data = json.loads(state_data)
                return data.get("code_verifier"), data.get("provider")
            return None, None
        except Exception:
            return None, None
    
    async def exchange_google_code(self, code: str, code_verifier: str) -> Dict[str, Any]:
        """Exchange Google authorization code for access token and user info"""
        async with httpx.AsyncClient() as client:
            # Exchange code for token
            token_response = await client.post(
                self.google_token_url,
                data={
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.OAUTH_REDIRECT_URL,
                    "code_verifier": code_verifier
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if token_response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Failed to exchange Google code: {token_response.text}"
                )
            
            token_data = token_response.json()
            access_token = token_data.get("access_token")
            
            if not access_token:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="No access token received from Google"
                )
            
            # Get user info
            user_response = await client.get(
                self.google_user_info_url,
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if user_response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Failed to get Google user info: {user_response.text}"
                )
            
            user_data = user_response.json()
            
            return {
                "email": user_data.get("email"),
                "name": user_data.get("name") or user_data.get("given_name", ""),
                "picture": user_data.get("picture"),
                "provider": "google",
                "provider_id": user_data.get("sub")
            }
    
    async def exchange_github_code(self, code: str, code_verifier: str) -> Dict[str, Any]:
        """Exchange GitHub authorization code for access token and user info"""
        async with httpx.AsyncClient() as client:
            # Exchange code for token
            token_response = await client.post(
                self.github_token_url,
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "client_secret": settings.GITHUB_CLIENT_SECRET,
                    "code": code,
                    # "code_verifier": code_verifier
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json"
                }
            )
            
            if token_response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Failed to exchange GitHub code: {token_response.text}"
                )
            
            print(f"token_response: {token_response.text}")
            print(f"token_response.json(): {token_response.json()}")
            token_data = token_response.json()
            access_token = token_data.get("access_token")
            
            print(f"access_token: {access_token}")
            
            if not access_token:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="No access token received from GitHub"
                )
            
            # Get user info
            user_response = await client.get(
                self.github_user_info_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json"
                }
            )
            
            if user_response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Failed to get GitHub user info: {user_response.text}"
                )
            
            user_data = user_response.json()
            
            # Get user email (may need separate call if email is private)
            email = user_data.get("email")
            if not email:
                email_response = await client.get(
                    "https://api.github.com/user/emails",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json"
                    }
                )
                if email_response.status_code == 200:
                    emails = email_response.json()
                    primary_email = next((e for e in emails if e.get("primary")), emails[0] if emails else None)
                    email = primary_email.get("email") if primary_email else None
            
            return {
                "email": email or f"{user_data.get('login')}@github.noreply",
                "name": user_data.get("name") or user_data.get("login", ""),
                "picture": user_data.get("avatar_url"),
                "provider": "github",
                "provider_id": str(user_data.get("id"))
            }

