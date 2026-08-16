"""Identity request/response contracts (PRD §25.1, §10.2)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from verity.platform.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]


class SignupRequest(BaseModel):
    email: EmailStr
    password: Password
    full_name: str | None = Field(default=None, max_length=200)
    #: IANA zone. Defaulted client-side from the browser (PRD PP17).
    timezone: str = "UTC"
    locale: str = "en-US"

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("must be a valid IANA timezone") from exc
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    device_name: str | None = Field(default=None, max_length=120)
    platform: Literal["web", "macos", "windows", "ios", "android"] = "web"
    app_version: str | None = Field(default=None, max_length=32)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    password: Password


class EmailVerificationConfirm(BaseModel):
    token: str = Field(min_length=16, max_length=512)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    locale: str
    timezone: str
    interview_locale: str
    email_verified: bool
    mfa_enabled: bool
    onboarding_state: dict[str, Any]
    created_at: dt.datetime


class AuthResponse(BaseModel):
    user: UserResponse
    tokens: TokenPair


class DeviceResponse(BaseModel):
    id: uuid.UUID
    name: str
    platform: str
    app_version: str | None
    trusted: bool
    last_seen_at: dt.datetime
    is_current: bool


class MessageResponse(BaseModel):
    """Deliberately non-committal for flows that must not confirm existence."""

    message: str


class UpdateProfileRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    locale: str | None = Field(default=None, max_length=20)
    timezone: str | None = Field(default=None, max_length=64)
    interview_locale: str | None = Field(default=None, max_length=20)
    onboarding_state: dict[str, Any] | None = None
