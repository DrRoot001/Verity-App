"""Operational CLI for durable defaults and first-admin bootstrap.

Normal setup never creates candidate or staff demo data. The first admin is an
explicit operator action with a caller-supplied password, and known legacy demo
accounts can be purged without touching real users.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from sqlalchemy import delete, or_, select

from verity.modules.admin.models import AuditLog, PlatformSetting, StaffMember, StaffRole
from verity.modules.admin.runtime import upsert_default_flags
from verity.modules.identity.models import User, UserStatus, normalize_email
from verity.modules.sessions.live_models import LiveSession, SessionEvent
from verity.platform.db.session import dispose_engine, transaction
from verity.platform.logging import configure_logging
from verity.platform.runtime_config import default_ai_settings
from verity.platform.security import hash_password

LEGACY_DEMO_EMAILS = (
    "admin@finalround.local",
    "admin@verity.dev",
    "support@verity.dev",
    "aiops@verity.dev",
    "security@verity.dev",
    "dana@verity.dev",
    "newuser@verity.dev",
)


async def seed_defaults() -> int:
    """Create operational defaults only; never create people or candidate data."""
    async with transaction() as db:
        await upsert_default_flags(db)
        existing = (
            await db.execute(
                select(PlatformSetting).where(
                    PlatformSetting.namespace == "ai", PlatformSetting.key == "gateway"
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                PlatformSetting(
                    namespace="ai",
                    key="gateway",
                    value=default_ai_settings(),
                    description="Model routing, budgets, and per-session generation limits.",
                )
            )
    print("Operational settings and feature flags are ready. No user accounts were created.")
    return 0


async def bootstrap_admin(email: str, password: str, name: str) -> int:
    if len(password) < 14:
        raise ValueError("Admin password must be at least 14 characters.")
    normalized = normalize_email(email)
    async with transaction() as db:
        user = (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()
        if user is None:
            user = User(email=normalized, full_name=name)
            db.add(user)
        user.full_name = name
        user.password_hash = hash_password(password)
        user.email_verified_at = user.email_verified_at or dt.datetime.now(dt.UTC)
        user.status = UserStatus.ACTIVE
        user.deleted_at = None
        await db.flush()

        grant = (
            await db.execute(
                select(StaffMember).where(
                    StaffMember.user_id == user.id, StaffMember.role == StaffRole.ADMIN
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            db.add(StaffMember(user_id=user.id, role=StaffRole.ADMIN))
        else:
            grant.revoked_at = None
    print(f"Administrator ready: {normalized}")
    return 0


async def create_user(email: str, password: str, name: str) -> int:
    """A plain candidate account, pre-verified.

    Pre-verifying is the whole point: a tester should not have to dig the
    confirmation link out of the outbox before they can sign in. It creates no
    resume, workspace or session — the account starts empty, exactly as a real
    signup does.
    """
    normalized = normalize_email(email)
    async with transaction() as db:
        user = (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()
        if user is None:
            user = User(email=normalized, full_name=name)
            db.add(user)
        user.full_name = name
        user.password_hash = hash_password(password)
        user.email_verified_at = user.email_verified_at or dt.datetime.now(dt.UTC)
        user.status = UserStatus.ACTIVE
        user.deleted_at = None
    print(f"Account ready: {normalized}")
    return 0


async def purge_demo() -> int:
    """Delete only the fixed legacy seed identities and all cascaded demo data."""
    async with transaction() as db:
        rows = list(
            (
                await db.execute(
                    select(User).where(
                        or_(
                            User.email.in_(LEGACY_DEMO_EMAILS), User.email.endswith("@verityqa.dev")
                        )
                    )
                )
            ).scalars()
        )
        ids = [row.id for row in rows]
        if ids:
            live_ids = list(
                (
                    await db.execute(select(LiveSession.id).where(LiveSession.user_id.in_(ids)))
                ).scalars()
            )
            if live_ids:
                await db.execute(delete(SessionEvent).where(SessionEvent.session_id.in_(live_ids)))
            await db.execute(
                delete(AuditLog).where(
                    (AuditLog.actor_id.in_(ids)) | (AuditLog.subject_user_id.in_(ids))
                )
            )
            await db.execute(delete(User).where(User.id.in_(ids)))
    print(f"Removed {len(ids)} legacy demo/test account(s).")
    return 0


async def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="verity")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="Create operational defaults; never creates accounts")
    bootstrap = sub.add_parser("bootstrap-admin", help="Create or reset the first administrator")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--password", required=True)
    bootstrap.add_argument("--name", default="FinalRound Administrator")
    create = sub.add_parser("create-user", help="Create or reset one plain candidate account")
    create.add_argument("--email", required=True)
    create.add_argument("--password", required=True)
    create.add_argument("--name", default="Test Candidate")
    sub.add_parser("purge-demo", help="Remove the fixed legacy demo identities")
    args = parser.parse_args()

    try:
        if args.command == "seed":
            return await seed_defaults()
        if args.command == "bootstrap-admin":
            return await bootstrap_admin(args.email, args.password, args.name)
        if args.command == "create-user":
            return await create_user(args.email, args.password, args.name)
        if args.command == "purge-demo":
            return await purge_demo()
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
