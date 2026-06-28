from datetime import datetime

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class AppleLoginRequest(BaseModel):
    identity_token: str
    display_name: str | None = None


class GoogleLoginRequest(BaseModel):
    access_token: str


class ForgotPasswordRequest(BaseModel):
    email: str


class VerifyCodeRequest(BaseModel):
    email: str
    code: str


class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool = False


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    avatar_emoji: str | None = None
    email_verified: bool = False
    created_at: datetime | None = None
    display_name_updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class UpdateMeRequest(BaseModel):
    display_name: str | None = None
    avatar_emoji: str | None = None


class UserStats(BaseModel):
    my_sightings: int
    unique_cats_spotted: int
    # Exploration ("Fog of Paw"): tiles uncovered and landmarks (checkpoints) lit.
    tiles_explored: int
    checkpoints_lit: int
    joined_at: datetime | None
