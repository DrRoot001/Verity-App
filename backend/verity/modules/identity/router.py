"""Auth and account endpoints (PRD §25.2 /auth, /users)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from verity.modules.identity.dependencies import (
    CurrentUser,
    RequestContextDep,
    SessionDep,
)
from verity.modules.identity.models import User
from verity.modules.identity.schemas import (
    AuthResponse,
    DeviceResponse,
    EmailVerificationConfirm,
    LoginRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
    UpdateProfileRequest,
    UserResponse,
)
from verity.modules.identity.service import (
    EmailAlreadyRegisteredError,
    IdentityService,
    IssuedTokens,
    RequestContext,
)
from verity.platform.config import settings
from verity.platform.email import provider as email_provider
from verity.platform.email import render
from verity.platform.logging import get_logger

log = get_logger("identity.router")

auth_router = APIRouter(prefix="/v1/auth", tags=["auth"])
users_router = APIRouter(prefix="/v1/users", tags=["users"])

#: Identical response for both outcomes of signup and password reset, so the
#: endpoint cannot be used to test whether an address is registered.
_CHECK_YOUR_EMAIL = "If that address can be used, we've sent an email with next steps."


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        locale=user.locale,
        timezone=user.timezone,
        interview_locale=user.interview_locale,
        email_verified=user.is_verified,
        mfa_enabled=user.mfa_enabled,
        onboarding_state=user.onboarding_state,
        created_at=user.created_at,
    )


def _token_pair(tokens: IssuedTokens) -> TokenPair:
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@auth_router.post("/signup", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def signup(
    payload: SignupRequest, session: SessionDep, ctx: RequestContextDep
) -> MessageResponse:
    service = IdentityService(session)
    try:
        _, verification = await service.signup(
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
            timezone=payload.timezone,
            locale=payload.locale,
            ctx=ctx,
        )
    except EmailAlreadyRegisteredError:
        # Same response as success, and the same amount of work, so neither the
        # body nor the timing reveals that the address is taken.
        return MessageResponse(message=_CHECK_YOUR_EMAIL)

    link = f"{settings.public_web_url}/verify-email?token={verification.plaintext}"
    await email_provider.send(render("verify_email", to=payload.email, link=link))
    return MessageResponse(message=_CHECK_YOUR_EMAIL)


@auth_router.post("/verify-email", response_model=UserResponse)
async def verify_email(payload: EmailVerificationConfirm, session: SessionDep) -> UserResponse:
    user = await IdentityService(session).verify_email(payload.token)
    return _user_response(user)


@auth_router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, session: SessionDep, ctx: RequestContextDep) -> AuthResponse:
    context = RequestContext(
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        device_name=payload.device_name or ctx.device_name,
        platform=payload.platform,
        app_version=payload.app_version or ctx.app_version,
    )
    user, tokens = await IdentityService(session).login(
        email=payload.email, password=payload.password, ctx=context
    )
    return AuthResponse(user=_user_response(user), tokens=_token_pair(tokens))


@auth_router.post("/refresh", response_model=AuthResponse)
async def refresh(
    payload: RefreshRequest, session: SessionDep, ctx: RequestContextDep
) -> AuthResponse:
    user, tokens = await IdentityService(session).refresh(
        refresh_token=payload.refresh_token, ctx=ctx
    )
    return AuthResponse(user=_user_response(user), tokens=_token_pair(tokens))


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(principal: CurrentUser, session: SessionDep) -> Response:
    await IdentityService(session).logout(principal.session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.post("/devices/revoke-all", response_model=MessageResponse)
async def logout_all(principal: CurrentUser, session: SessionDep) -> MessageResponse:
    revoked = await IdentityService(session).logout_all(principal.user.id)
    return MessageResponse(message=f"Signed out of {revoked} session(s).")


@auth_router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(principal: CurrentUser, session: SessionDep) -> list[DeviceResponse]:
    service = IdentityService(session)
    devices = await service.list_devices(principal.user.id)
    return [
        DeviceResponse(
            id=d.id,
            name=d.name,
            platform=d.platform,
            app_version=d.app_version,
            trusted=d.trusted,
            last_seen_at=d.last_seen_at,
            is_current=False,
        )
        for d in devices
    ]


@auth_router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> Response:
    await IdentityService(session).revoke_device(principal.user.id, device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.post("/password/reset-request", response_model=MessageResponse)
async def request_password_reset(
    payload: PasswordResetRequest, session: SessionDep, ctx: RequestContextDep
) -> MessageResponse:
    token = await IdentityService(session).request_password_reset(payload.email, ctx)
    if token is not None:
        link = f"{settings.public_web_url}/reset-password?token={token.plaintext}"
        await email_provider.send(render("password_reset", to=payload.email, link=link))
    return MessageResponse(message=_CHECK_YOUR_EMAIL)


@auth_router.post("/password/reset", response_model=MessageResponse)
async def confirm_password_reset(
    payload: PasswordResetConfirm, session: SessionDep
) -> MessageResponse:
    await IdentityService(session).confirm_password_reset(
        token=payload.token, password=payload.password
    )
    return MessageResponse(message="Password updated. Please sign in again.")


# ── /users ───────────────────────────────────────────────────────────


@users_router.get("/me", response_model=UserResponse)
async def get_me(principal: CurrentUser) -> UserResponse:
    return _user_response(principal.user)


@users_router.patch("/me", response_model=UserResponse)
async def update_me(
    payload: UpdateProfileRequest, principal: CurrentUser, session: SessionDep
) -> UserResponse:
    user = principal.user
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(user, field, value)
    await session.flush()
    return _user_response(user)


@users_router.post("/me/delete", response_model=MessageResponse)
async def request_deletion(principal: CurrentUser, session: SessionDep) -> MessageResponse:
    executes_at = await IdentityService(session).request_deletion(principal.user.id)
    return MessageResponse(
        message=f"Account scheduled for deletion on {executes_at.date().isoformat()}. "
        "Sign in before then to cancel."
    )
