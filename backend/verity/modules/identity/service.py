"""Identity service (PRD §10.2, §28.2).

Holds the auth rules that must not be duplicated in routers: enumeration-safe
responses, refresh-token rotation with reuse detection, and device tracking.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.identity.models import (
    AuthSession,
    Device,
    User,
    UserStatus,
    normalize_email,
)
from verity.platform.config import settings
from verity.platform.errors import AppError, ErrorCode, RecoveryAction
from verity.platform.logging import get_logger
from verity.platform.ratelimit import RateLimitScope, limiter
from verity.platform.security import (
    OpaqueToken,
    dummy_verify,
    generate_token,
    hash_ip,
    hash_password,
    hash_token,
    issue_access_token,
    needs_rehash,
    verify_password,
)

log = get_logger("identity.service")


def _as_text(value: bytes | str) -> str:
    """Redis clients may or may not decode responses; normalize once."""
    return value.decode() if isinstance(value, bytes) else value


VERIFICATION_TOKEN_TTL = dt.timedelta(hours=24)
RESET_TOKEN_TTL = dt.timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Ambient request facts the service needs but must not reach into."""

    ip: str | None = None
    user_agent: str | None = None
    device_name: str | None = None
    platform: str = "web"
    app_version: str | None = None


class IdentityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Registration ─────────────────────────────────────────────────

    async def signup(
        self,
        *,
        email: str,
        password: str,
        full_name: str | None,
        timezone: str,
        locale: str,
        ctx: RequestContext,
    ) -> tuple[User, OpaqueToken]:
        await limiter.enforce(RateLimitScope.SIGNUP_IP, ctx.ip or "unknown")

        normalized = normalize_email(email)
        user = User(
            email=normalized,
            password_hash=hash_password(password),
            full_name=full_name,
            timezone=timezone,
            locale=locale,
            interview_locale=locale,
        )
        self._session.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # PRD §28.3: never confirm whether an address is registered. The
            # caller returns the same "check your email" response either way.
            await self._session.rollback()
            log.info("signup_duplicate_email_suppressed")
            raise EmailAlreadyRegisteredError() from exc

        verification = generate_token()
        await self._store_verification_token(user.id, verification)
        log.info("user_signed_up", user_id=str(user.id))
        return user, verification

    async def _store_verification_token(self, user_id: uuid.UUID, token: OpaqueToken) -> None:
        from verity.platform.cache import RedisRole, get_redis

        await get_redis(RedisRole.SESSION).set(
            f"verify:{token.digest.hex()}",
            str(user_id),
            ex=int(VERIFICATION_TOKEN_TTL.total_seconds()),
        )

    async def verify_email(self, token: str) -> User:
        from verity.platform.cache import RedisRole, get_redis

        redis = get_redis(RedisRole.SESSION)
        key = f"verify:{hash_token(token).hex()}"
        raw_user_id = await redis.get(key)
        if raw_user_id is None:
            raise AppError(
                ErrorCode.TOKEN_INVALID,
                "This verification link is no longer valid.",
                recovery_action=RecoveryAction(
                    type="verify_email", target="/verify-email", label="Send a new link"
                ),
            )
        await redis.delete(key)

        user = await self._require_user(uuid.UUID(_as_text(raw_user_id)))
        if user.email_verified_at is None:
            user.email_verified_at = dt.datetime.now(dt.UTC)
            await self._session.flush()
        return user

    # ── Login ────────────────────────────────────────────────────────

    async def login(
        self, *, email: str, password: str, ctx: RequestContext
    ) -> tuple[User, IssuedTokens]:
        normalized = normalize_email(email)
        await limiter.enforce(RateLimitScope.LOGIN_IP, ctx.ip or "unknown")
        await limiter.enforce(RateLimitScope.LOGIN_IDENTITY, normalized)

        user = (
            await self._session.execute(
                select(User).where(User.email == normalized, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()

        if user is None or user.password_hash is None:
            # Equalize timing so a missing account is indistinguishable from a
            # wrong password (PRD §28.3 account enumeration).
            dummy_verify()
            raise _invalid_credentials()

        if not verify_password(password, user.password_hash):
            raise _invalid_credentials()

        if user.status == UserStatus.SUSPENDED:
            raise AppError(ErrorCode.ACCOUNT_SUSPENDED, "This account is suspended.")
        if user.status == UserStatus.PENDING_DELETION:
            # Signing in cancels a pending deletion — the user clearly wants the
            # account (PRD §10.2 delete-account grace period).
            user.status = UserStatus.ACTIVE
            user.deletion_requested_at = None

        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        await limiter.reset(RateLimitScope.LOGIN_IDENTITY, normalized)
        tokens = await self._issue_session(user, ctx)
        log.info("user_logged_in", user_id=str(user.id))
        return user, tokens

    # ── Sessions and rotation ────────────────────────────────────────

    async def _issue_session(
        self,
        user: User,
        ctx: RequestContext,
        *,
        family_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
    ) -> IssuedTokens:
        device_id = device_id or (await self._upsert_device(user.id, ctx)).id
        refresh = generate_token()
        now = dt.datetime.now(dt.UTC)

        auth_session = AuthSession(
            user_id=user.id,
            device_id=device_id,
            family_id=family_id or uuid.uuid4(),
            refresh_token_hash=refresh.digest,
            expires_at=now + dt.timedelta(seconds=settings.refresh_token_ttl_seconds),
            ip_hash=hash_ip(ctx.ip),
            user_agent=ctx.user_agent,
        )
        self._session.add(auth_session)
        await self._session.flush()

        access = issue_access_token(user_id=user.id, session_id=auth_session.id)
        return IssuedTokens(
            access_token=access,
            refresh_token=refresh.plaintext,
            expires_in=settings.access_token_ttl_seconds,
            session_id=auth_session.id,
        )

    async def refresh(
        self, *, refresh_token: str, ctx: RequestContext
    ) -> tuple[User, IssuedTokens]:
        await limiter.enforce(RateLimitScope.TOKEN_REFRESH, ctx.ip or "unknown")

        digest = hash_token(refresh_token)
        record = (
            await self._session.execute(
                select(AuthSession).where(AuthSession.refresh_token_hash == digest)
            )
        ).scalar_one_or_none()

        if record is None:
            raise AppError(ErrorCode.TOKEN_INVALID, "This session is no longer valid.")

        now = dt.datetime.now(dt.UTC)

        if record.revoked_at is not None:
            # A revoked token being replayed means it was captured after
            # rotation. Kill the whole family — we cannot tell attacker from
            # victim, so neither keeps access (PRD §28.2 reuse detection).
            await self._revoke_family(record.family_id, reason="reuse_detected")
            # Commit before raising. The request-scoped unit of work rolls back
            # on exception, which would silently discard the revocation and
            # leave the stolen family live — the exact opposite of the intent.
            await self._session.commit()
            log.warning("refresh_token_reuse_detected", user_id=str(record.user_id))
            raise AppError(
                ErrorCode.TOKEN_REUSE_DETECTED,
                "For your security, all sessions were signed out.",
            )

        if record.expires_at <= now:
            raise AppError(ErrorCode.TOKEN_EXPIRED, "Your session has expired.")

        record.revoked_at = now
        record.revoked_reason = "rotated"

        user = await self._require_user(record.user_id)
        if user.status != UserStatus.ACTIVE:
            raise AppError(ErrorCode.ACCOUNT_SUSPENDED, "This account is not active.")

        tokens = await self._issue_session(
            user, ctx, family_id=record.family_id, device_id=record.device_id
        )
        return user, tokens

    async def logout(self, session_id: uuid.UUID) -> None:
        await self._session.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="logout")
        )

    async def logout_all(self, user_id: uuid.UUID) -> int:
        """PRD FR-AUTH-007: revoke every refresh family for this user."""
        result = await self._session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="logout_all")
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def _revoke_family(self, family_id: uuid.UUID, *, reason: str) -> None:
        await self._session.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason=reason)
        )

    # ── Devices ──────────────────────────────────────────────────────

    async def _upsert_device(self, user_id: uuid.UUID, ctx: RequestContext) -> Device:
        name = ctx.device_name or f"{ctx.platform.title()} device"
        existing = (
            await self._session.execute(
                select(Device).where(
                    Device.user_id == user_id,
                    Device.name == name,
                    Device.platform == ctx.platform,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.last_seen_at = dt.datetime.now(dt.UTC)
            existing.app_version = ctx.app_version or existing.app_version
            await self._session.flush()
            return existing

        device = Device(
            user_id=user_id, name=name, platform=ctx.platform, app_version=ctx.app_version
        )
        self._session.add(device)
        await self._session.flush()
        return device

    async def list_devices(self, user_id: uuid.UUID) -> list[Device]:
        rows = await self._session.execute(
            select(Device).where(Device.user_id == user_id).order_by(Device.last_seen_at.desc())
        )
        return list(rows.scalars())

    async def revoke_device(self, user_id: uuid.UUID, device_id: uuid.UUID) -> None:
        device = (
            await self._session.execute(
                select(Device).where(Device.id == device_id, Device.user_id == user_id)
            )
        ).scalar_one_or_none()
        if device is None:
            raise AppError.not_found("Device")

        await self._session.execute(
            update(AuthSession)
            .where(AuthSession.device_id == device_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason="device_revoked")
        )
        await self._session.delete(device)

    # ── Password reset ───────────────────────────────────────────────

    async def request_password_reset(self, email: str, ctx: RequestContext) -> OpaqueToken | None:
        """Returns a token only when the account exists.

        The router sends the same response either way; returning ``None`` lets
        the caller skip the email without branching on existence in the response.
        """
        normalized = normalize_email(email)
        await limiter.enforce(RateLimitScope.PASSWORD_RESET, normalized)

        user = (
            await self._session.execute(
                select(User).where(User.email == normalized, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if user is None:
            return None

        from verity.platform.cache import RedisRole, get_redis

        token = generate_token()
        await get_redis(RedisRole.SESSION).set(
            f"reset:{token.digest.hex()}",
            str(user.id),
            ex=int(RESET_TOKEN_TTL.total_seconds()),
        )
        return token

    async def confirm_password_reset(self, *, token: str, password: str) -> User:
        from verity.platform.cache import RedisRole, get_redis

        redis = get_redis(RedisRole.SESSION)
        key = f"reset:{hash_token(token).hex()}"
        raw_user_id = await redis.get(key)
        if raw_user_id is None:
            raise AppError(
                ErrorCode.TOKEN_INVALID,
                "This reset link has expired.",
                recovery_action=RecoveryAction(
                    type="retry", target="/reset-password", label="Request a new link"
                ),
            )
        await redis.delete(key)

        user = await self._require_user(uuid.UUID(_as_text(raw_user_id)))
        user.password_hash = hash_password(password)
        # PRD FR-AUTH-004: completing a reset invalidates every existing session.
        await self.logout_all(user.id)
        await self._session.flush()
        log.info("password_reset_completed", user_id=str(user.id))
        return user

    # ── Account lifecycle ────────────────────────────────────────────

    async def request_deletion(self, user_id: uuid.UUID) -> dt.datetime:
        user = await self._require_user(user_id)
        user.status = UserStatus.PENDING_DELETION
        user.deletion_requested_at = dt.datetime.now(dt.UTC)
        await self.logout_all(user_id)
        await self._session.flush()
        return user.deletion_requested_at + dt.timedelta(days=7)

    async def cancel_deletion(self, user_id: uuid.UUID) -> None:
        user = await self._require_user(user_id)
        user.status = UserStatus.ACTIVE
        user.deletion_requested_at = None
        await self._session.flush()

    async def _require_user(self, user_id: uuid.UUID) -> User:
        user = (
            await self._session.execute(
                select(User).where(User.id == user_id, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if user is None:
            raise AppError.not_found("Account")
        return user


class EmailAlreadyRegisteredError(Exception):
    """Internal signal. Never surfaces to the client (PRD §28.3)."""


def _invalid_credentials() -> AppError:
    return AppError(
        ErrorCode.INVALID_CREDENTIALS,
        "That email or password is incorrect.",
        recovery_action=RecoveryAction(type="edit_input", label="Check your details"),
    )
