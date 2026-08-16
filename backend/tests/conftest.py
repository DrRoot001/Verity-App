from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Tests must never inherit developer credentials or point at a real database.
os.environ.setdefault("VERITY_ENV", "local")
os.environ.setdefault("VERITY_LOG_FORMAT", "console")
os.environ.setdefault("VERITY_LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def api_client() -> Iterator[object]:
    from fastapi.testclient import TestClient

    from verity.apps.api.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client
