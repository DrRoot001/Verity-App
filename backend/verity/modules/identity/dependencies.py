"""Authentication dependencies (PRD §28.2).

``authorize(actor, action, resource)`` lives here as the single place a request
is turned into an authenticated principal. Routers never parse tokens.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.identity.models import AuthSession, User, UserStatus
from verity.modules.identity.service import RequestContext
from verity.platform.db.session import session_dependency
from verity.platform.errors import AppError, ErrorCode, RecoveryAction
from verity.platform.logging import set_request_context
from verity.platform.security import TokenAudience, decode_access_token

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor for one request."""

    user: User
    session_id: uuid.UUID

    @property
    def id(self) -> uuid.UUID:
        return self.user.id


def request_context(request: Request) -> RequestContext:
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (
        request.client.host if request.client else None
    )
    return RequestContext(
        ip=client_ip,
        user_agent=request.headers.get("User-Agent"),
        device_name=request.headers.get("X-Device-Name"),
        platform=request.headers.get("X-Platform", "web"),
        app_version=request.headers.get("X-App-Version"),
    )


RequestContextDep = Annotated[RequestContext, Depends(request_context)]


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AppError(
            ErrorCode.UNAUTHENTICATED,
            "Sign in to continue.",
            recovery_action=RecoveryAction(type="reauthenticate", target="/login"),
        )
    return token


async def current_principal(request: Request, session: SessionDep) -> Principal:
    """Resolve the caller, rejecting tokens whose session was revoked.

    Checking the session row on every request is what makes "log out all
    devices" (FR-AUTH-007) take effect immediately rather than at access-token
    expiry.
    """
    claims = decode_access_token(_bearer_token(request), audience=TokenAudience.ACCESS)

    auth_session = (
        await session.execute(select(AuthSession).where(AuthSession.id == claims.session_id))
    ).scalar_one_or_none()

    if auth_session is None or auth_session.revoked_at is not None:
        raise AppError(
            ErrorCode.TOKEN_INVALID,
            "This session has ended. Please sign in again.",
            recovery_action=RecoveryAction(type="reauthenticate", target="/login"),
        )
    if auth_session.expires_at <= dt.datetime.now(dt.UTC):
        raise AppError(ErrorCode.TOKEN_EXPIRED, "Your session has expired.")

    user = (
        await session.execute(
            select(User).where(User.id == claims.user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()

    if user is None:
        raise AppError(ErrorCode.TOKEN_INVALID, "This account is no longer available.")
    if user.status == UserStatus.SUSPENDED:
        raise AppError(ErrorCode.ACCOUNT_SUSPENDED, "This account is suspended.")

    set_request_context(user_id=str(user.id))
    return Principal(user=user, session_id=auth_session.id)


CurrentUser = Annotated[Principal, Depends(current_principal)]


async def verified_principal(principal: CurrentUser) -> Principal:
    """Gate for AI operations (PRD FR-AUTH-001).

    Verification gates AI work, not browsing — so this is a separate dependency
    rather than a blanket rule on every authenticated route.
    """
    if not principal.user.can_use_ai:
        raise AppError(
            ErrorCode.EMAIL_NOT_VERIFIED,
            "Verify your email address to use this feature.",
            recovery_action=RecoveryAction(
                type="verify_email", target="/verify-email", label="Resend verification"
            ),
        )
    return principal


VerifiedUser = Annotated[Principal, Depends(verified_principal)]
