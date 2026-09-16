"""Proxy-auth beta boundary contracts."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


@dataclass(frozen=True)
class FakeBatch:
    batch_id: str
    owner_session_id: str
    expected_file_count: int

    def snapshot(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "owner_session_id": self.owner_session_id,
            "expected_file_count": self.expected_file_count,
            "finalized_file_count": 0,
            "status": "open",
            "created_at": None,
            "sealed_at": None,
            "expires_at": None,
        }


class FakeBatchStore:
    def __init__(self) -> None:
        self.created_owner_session_id: str | None = None

    def create_batch(self, *, owner_session_id: str, expected_file_count: int) -> FakeBatch:
        self.created_owner_session_id = owner_session_id
        return FakeBatch("BATCH-1", owner_session_id, expected_file_count)


def test_proxy_auth_requires_injected_user() -> None:
    client = TestClient(app)
    env = {
        "APP_REQUIRE_PROXY_AUTH": "1",
        "APP_TRUSTED_PROXY_IPS": "testclient,127.0.0.1,::1",
    }

    with patch.dict("os.environ", env, clear=False):
        response = client.get("/documents/healthz")

    assert response.status_code == 401


def test_proxy_auth_accepts_trusted_proxy_user() -> None:
    client = TestClient(app)
    env = {
        "APP_REQUIRE_PROXY_AUTH": "1",
        "APP_TRUSTED_PROXY_IPS": "testclient,127.0.0.1,::1",
    }

    with patch.dict("os.environ", env, clear=False):
        response = client.get("/documents/healthz", headers={"x-app-authenticated-user": "user@example.com"})

    assert response.status_code == 200


def test_proxy_auth_overrides_ingestion_batch_owner() -> None:
    client = TestClient(app)
    store = FakeBatchStore()
    user_id = "beta.user.with.a.long.email.identifier.for.auth.testing@example.com"
    env = {
        "APP_REQUIRE_PROXY_AUTH": "1",
        "APP_TRUSTED_PROXY_IPS": "testclient,127.0.0.1,::1",
    }

    with (
        patch.dict("os.environ", env, clear=False),
        patch("app.api.routes_ingestion._job_store", return_value=store),
    ):
        response = client.post(
            "/ingestion/batches",
            json={"owner_session_id": "browser-session", "expected_file_count": 1},
            headers={"x-app-authenticated-user": user_id},
        )

    assert response.status_code == 200
    assert response.json()["owner_session_id"] == user_id
    assert store.created_owner_session_id == user_id
