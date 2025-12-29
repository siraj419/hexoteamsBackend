from pydantic import BaseModel, EmailStr, field_validator
from datetime import datetime
from typing import Optional
import re




class AuthLoginRequest(BaseModel):
    email: EmailStr
    password: str

class ValidatedPassword(BaseModel):
    password: str
    
    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """
        Validate password strength:
        - Minimum 8 characters
        - At least one uppercase letter
        - At least one lowercase letter
        - At least one digit
        - At least one special character
        """
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        
        if not re.search(r'[a-z]', v):
            raise ValueError('Password must contain at least one lowercase letter')
        
        if not re.search(r'\d', v):
            raise ValueError('Password must contain at least one digit')
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~;]', v):
            raise ValueError('Password must contain at least one special character (!@#$%^&*(),.?":{}|<>_-+=[]\\\/`~;)')
        
        return v 

class AuthRegisterRequest(ValidatedPassword):
    display_name: str
    email: EmailStr
        

class AuthForgetPasswordRequest(BaseModel):
    email: EmailStr
    # Optional redirect URL so Supabase sends the user back to the frontend
    redirect_to: Optional[str] = None

class AuthResetPasswordRequest(ValidatedPassword):
    access_token: str
    refresh_token: str

class User(BaseModel):
    id: str
    display_name: str
    email: EmailStr
    timezone: str
    avatar_url: Optional[str] = None
    browser_notifications: bool
    created_at: datetime
    updated_at: datetime


class AuthLogoutResponse(BaseModel):
    message: str

class AuthConfirmRequest(BaseModel):
    access_token: str
    access_token_expires_in: int
    refresh_token: str


# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
# ^^^^^^^^^^^^^^^^^^^^^^^^^ RESPONSE SCHEMAS ^^^^^^^^^^^^^^^^^^^^^^^^^
# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^


class AuthRegisterResponse(BaseModel):
    message: str
    
class AuthTokenResponse(BaseModel):
    token_type: str = "Bearer"
    access_token: str
    expires_in: int

class AuthLoginResponse(AuthTokenResponse):
    pass
class AuthConfirmResponse(AuthTokenResponse):
    pass

class AuthRefreshTokenResponse(AuthTokenResponse):
    pass

class AuthForgetPasswordResponse(BaseModel):
    message: str

class AuthResetPasswordResponse(AuthTokenResponse):
    pass

class AuthChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    
    @field_validator('new_password')
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """
        Validate password strength:
        - Minimum 8 characters
        - At least one uppercase letter
        - At least one lowercase letter
        - At least one digit
        - At least one special character
        """
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        
        if not re.search(r'[a-z]', v):
            raise ValueError('Password must contain at least one lowercase letter')
        
        if not re.search(r'\d', v):
            raise ValueError('Password must contain at least one digit')
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~;]', v):
            raise ValueError('Password must contain at least one special character (!@#$%^&*(),.?":{}|<>_-+=[]\\\/`~;)')
        
        return v

class AuthChangePasswordResponse(BaseModel):
    message: str

class AuthUpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    timezone: Optional[str] = None
    browser_notifications: Optional[bool] = None
    email_notifications: Optional[bool] = None

class AuthUpdateProfileResponse(BaseModel):
    message: str

class AuthChangeAvatarResponse(BaseModel):
    avatar_url: str

class AuthRemoveAvatarResponse(BaseModel):
    message: str