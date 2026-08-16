from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Tests must never inherit developer credentials or point at a real database.
os.environ.setdefault("VERITY_ENV", "local")
os.environ.setdefault("VERITY_LOG_FORMAT", "console")
os.environ.setdefault("VERITY_LOG_LEVEL", "WARNING")
# Argon2 at production cost (64 MiB x3) makes an auth suite take minutes. The
# parameters are configuration, so lowering them here exercises the identical
# code path without weakening the deployed setting.
os.environ.setdefault("VERITY_ARGON2_MEMORY_KIB", "8192")
os.environ.setdefault("VERITY_ARGON2_TIME_COST", "1")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def api_client() -> Iterator[object]:
    from fastapi.testclient import TestClient

    from verity.apps.api.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client
