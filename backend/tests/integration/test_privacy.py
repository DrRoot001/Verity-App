"""Account export and deletion (PRD §29.4, AC-PRIV-001).

The deletion test builds a user with data in every store — graph rows, vectors,
a stored document, a workspace, a live session — and then asserts that nothing
answers afterwards. Asserting only that the ``users`` row is gone would pass
against a pipeline that orphans everything else.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from verity.modules.privacy.models import GRACE_PERIOD_DAYS, DeletionStatus

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple-7"

RESUME = """
Dana Whitfield

WORK EXPERIENCE

Site Reliability Engineer at Northwind Systems
March 2021 - Present
- Led incident response for a payments outage, reducing MTTR from 42 minutes to 9 minutes

TECHNICAL SKILLS
Python, PostgreSQL, Kubernetes
"""


class Citizen:
    """A user with data in every store the pipeline must clear."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.email = f"privacy-{uuid.uuid4().hex[:10]}@verityqa.dev"
        ip = {"X-Forwarded-For": f"198.18.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"}

        client.post(
            "/v1/auth/signup",
            json={"email": self.email, "password": PASSWORD, "full_name": "Dana"},
            headers=ip,
        )
        self._verify()
        tokens = client.post(
            "/v1/auth/login", json={"email": self.email, "password": PASSWORD}, headers=ip
        ).json()["tokens"]
        self.headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        self.id = client.get("/v1/users/me", headers=self.headers).json()["id"]

    def _verify(self) -> None:
        matches = sorted(
            Path(".data/outbox").glob(f"{self.email}-*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        token = re.search(r"verify-email\?token=([\w\-]+)", matches[0].read_text())
        assert token
        self.client.post("/v1/auth/verify-email", json={"token": token.group(1)})

    def populate(self) -> None:
        self.client.post("/v1/resumes/paste", json={"raw_text": RESUME}, headers=self.headers)
        review = self.client.get("/v1/profile/review", headers=self.headers).json()
        for entity_type in ("experience", "skill", "achievement"):
            ids = [n["id"] for n in review["pending"] if n["entity_type"] == entity_type]
            if ids:
                self.client.post(
                    "/v1/profile/bulk-approve",
                    json={"entity_type": entity_type, "ids": ids},
                    headers=self.headers,
                )
        workspace = self.client.post(
            "/v1/workspaces",
            json={"company_name": "Northwind", "role_title": "SRE"},
            headers=self.headers,
        ).json()
        self.client.post(
            "/v1/live-sessions", json={"workspace_id": workspace["id"]}, headers=self.headers
        )


@pytest.fixture
def zero_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the pipeline without waiting seven real days.

    The grace period is configuration, so this exercises the same code path a
    zero-grace environment would.
    """
    import verity.modules.privacy.service as service

    monkeypatch.setattr(service, "GRACE_PERIOD_DAYS", 0)


@pytest.fixture
def citizen(api_client: Any) -> Citizen:
    person = Citizen(api_client)
    person.populate()
    return person


# ── Export (FR-PRIV-022) ─────────────────────────────────────────────


def test_export_contains_the_users_own_data(citizen: Citizen) -> None:
    export = citizen.client.get("/v1/account/export", headers=citizen.headers).json()

    assert export["account"]["email"] == citizen.email
    assert export["candidate_graph"]["experiences"]
    assert export["workspaces"]


def test_export_never_includes_the_password_hash(citizen: Citizen) -> None:
    export = citizen.client.get("/v1/account/export", headers=citizen.headers).json()
    assert "password_hash" not in str(export)


def test_export_requires_authentication(api_client: Any) -> None:
    assert api_client.get("/v1/account/export").status_code == 401


# ── Deletion request (FR-AUTH-009) ───────────────────────────────────


def test_deletion_requires_typing_the_account_email(citizen: Citizen) -> None:
    response = citizen.client.post(
        "/v1/account/deletion",
        json={"confirm_email": "someone-else@verityqa.dev"},
        headers=citizen.headers,
    )

    assert response.status_code == 400


def test_a_deletion_request_starts_a_grace_period(citizen: Citizen) -> None:
    response = citizen.client.post(
        "/v1/account/deletion",
        json={"confirm_email": citizen.email},
        headers=citizen.headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["grace_period_days"] == GRACE_PERIOD_DAYS
    assert body["cancellable"] is True
    # The account is not gone yet — that is the whole point of the grace period.
    assert citizen.client.get("/v1/users/me", headers=citizen.headers).status_code == 200


def test_a_deletion_can_be_cancelled_during_the_grace_period(citizen: Citizen) -> None:
    citizen.client.post(
        "/v1/account/deletion",
        json={"confirm_email": citizen.email},
        headers=citizen.headers,
    )

    cancelled = citizen.client.delete("/v1/account/deletion", headers=citizen.headers)
    assert cancelled.status_code == 204

    export = citizen.client.get("/v1/account/export", headers=citizen.headers).json()
    assert export["account"]["status"] == "active"


# ── AC-PRIV-001 ──────────────────────────────────────────────────────


def _staff(client: Any) -> Citizen:
    """An admin, granted in the database — there is no API that mints staff."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from verity.platform.config import settings

    person = Citizen(client)

    async def grant() -> None:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO staff_members (id, user_id, role, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :uid, 'admin', now(), now())"
                    ),
                    {"uid": person.id},
                )
        finally:
            await engine.dispose()

    asyncio.run(grant())
    return person


def test_execution_clears_every_store(api_client: Any, zero_grace: None) -> None:
    """The acceptance criterion.

    The service verifies each store itself and fails the job if anything still
    answers, so a ``completed`` status is the assertion — plus the account
    genuinely being unreachable afterwards.
    """
    citizen = Citizen(api_client)
    citizen.populate()
    admin = _staff(api_client)

    citizen.client.post(
        "/v1/account/deletion",
        json={"confirm_email": citizen.email},
        headers=citizen.headers,
    )

    run = admin.client.post("/v1/account/deletion/run", headers=admin.headers)
    assert run.status_code == 200, run.text
    assert citizen.id in run.json()["executed"]

    # The account and everything it owned are gone.
    assert citizen.client.get("/v1/users/me", headers=citizen.headers).status_code == 401
    found = admin.client.get(f"/v1/admin/users?q={citizen.email}", headers=admin.headers).json()
    assert found == []

    # Signing in again is not possible: the credential row went with the user.
    again = api_client.post("/v1/auth/login", json={"email": citizen.email, "password": PASSWORD})
    assert again.status_code == 401


def test_a_store_that_still_answers_fails_the_job(
    api_client: Any, zero_grace: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification is what makes `completed` mean anything.

    With one store lying about being empty, the job must fail rather than
    report a deletion that did not happen.
    """
    from verity.platform.storage import LocalObjectStorage

    async def still_there(self: Any, prefix: str) -> list[str]:
        return ["leftover-object"]

    monkeypatch.setattr(LocalObjectStorage, "list_prefix", still_there)

    citizen = Citizen(api_client)
    admin = _staff(api_client)
    citizen.client.post(
        "/v1/account/deletion",
        json={"confirm_email": citizen.email},
        headers=citizen.headers,
    )

    run = admin.client.post("/v1/account/deletion/run", headers=admin.headers)

    assert run.status_code != 200, "a job that could not clear a store must not succeed"


def test_deletion_is_recorded_after_the_user_is_gone(api_client: Any, zero_grace: None) -> None:
    """The job outlives its subject — the audit trail cannot cascade away."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from verity.platform.config import settings

    citizen = Citizen(api_client)
    admin = _staff(api_client)
    citizen.client.post(
        "/v1/account/deletion",
        json={"confirm_email": citizen.email},
        headers=citizen.headers,
    )
    admin.client.post("/v1/account/deletion/run", headers=admin.headers)

    async def read() -> Any:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return (
                    await conn.execute(
                        text(
                            "SELECT status, stores, email FROM deletion_jobs WHERE user_id = :uid"
                        ),
                        {"uid": citizen.id},
                    )
                ).one()
        finally:
            await engine.dispose()

    status, stores, email = asyncio.run(read())

    assert status == DeletionStatus.COMPLETED
    assert email == citizen.email
    # Per-store timestamps, as the criterion requires.
    for store in ("postgres", "vectors", "object_storage", "redis"):
        assert stores[store]["at"], f"{store} must record when it was cleared"
