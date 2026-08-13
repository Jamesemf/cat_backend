from pydantic import BaseModel, Field

from app.schemas.media import UtcDatetimeOpt


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
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
    new_password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool = False
    # True when the account has never completed onboarding (users.onboarded_at is
    # null), so the app sends it through the intro carousel and profile setup
    # rather than straight to the tabs. Unlike is_new_user this survives a
    # reinstall and covers every sign-in method, including email/password.
    needs_onboarding: bool = False


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    avatar_emoji: str | None = None
    email_verified: bool = False
    created_at: UtcDatetimeOpt = None
    display_name_updated_at: UtcDatetimeOpt = None

    model_config = {"from_attributes": True}


class UpdateMeRequest(BaseModel):
    display_name: str | None = None
    avatar_emoji: str | None = None


class UserStats(BaseModel):
    my_sightings: int
    unique_cats_spotted: int
    # Exploration ("Fog of Paw"): tiles uncovered.
    tiles_explored: int
    joined_at: UtcDatetimeOpt
