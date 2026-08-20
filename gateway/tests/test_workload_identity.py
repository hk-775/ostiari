"""Rotating gateway workload credential contracts."""

from __future__ import annotations

import pytest
from ostiari_gateway import workload_identity
from ostiari_gateway.workload_identity import (
    WorkloadCredentialError,
    machine_headers,
    reset_workload_credentials,
    validate_production_credential,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_credentials():
    reset_workload_credentials()
    yield
    reset_workload_credentials()


async def test_token_file_is_reread_for_rotation(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("token-one\n", encoding="utf-8")
    monkeypatch.delenv("OSTIARI_ENV", raising=False)
    monkeypatch.setenv("OSTIARI_WORKLOAD_TOKEN_FILE", str(token_file))

    assert await machine_headers() == {"Authorization": "Bearer token-one"}

    token_file.write_text("token-two\n", encoding="utf-8")
    assert await machine_headers() == {"Authorization": "Bearer token-two"}


async def test_development_legacy_headers_remain_compatible(monkeypatch):
    monkeypatch.delenv("OSTIARI_ENV", raising=False)
    monkeypatch.delenv("OSTIARI_WORKLOAD_TOKEN_FILE", raising=False)
    monkeypatch.delenv("OSTIARI_WORKLOAD_TOKEN", raising=False)
    monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "service-secret")
    monkeypatch.setenv("OSTIARI_INGEST_KEY", "ingest-secret")

    assert await machine_headers() == {
        "X-Ostiari-Service-Key": "service-secret"
    }
    assert await machine_headers(legacy="ingest") == {
        "X-Ingest-Key": "ingest-secret"
    }


def test_production_requires_rotating_token_file(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.delenv("OSTIARI_WORKLOAD_TOKEN_FILE", raising=False)
    monkeypatch.delenv("OSTIARI_WORKLOAD_TOKEN", raising=False)

    with pytest.raises(
        WorkloadCredentialError,
        match="OSTIARI_WORKLOAD_TOKEN_FILE",
    ):
        validate_production_credential()


def test_production_rejects_static_token(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("rotating-token", encoding="utf-8")
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_WORKLOAD_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("OSTIARI_WORKLOAD_TOKEN", "static-token")

    with pytest.raises(WorkloadCredentialError, match="forbidden"):
        validate_production_credential()


async def test_oauth_client_credentials_are_cached(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv(
        "OSTIARI_WORKLOAD_TOKEN_URL",
        "https://identity.example/oauth2/token",
    )
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_ID", "gateway-a")
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_SECRET", "client-secret")
    calls = 0

    async def issue(_config):
        nonlocal calls
        calls += 1
        return f"token-{calls}", 300.0

    monkeypatch.setattr(workload_identity, "_request_oauth_token", issue)

    assert await machine_headers() == {"Authorization": "Bearer token-1"}
    assert await machine_headers() == {"Authorization": "Bearer token-1"}
    assert calls == 1


async def test_oauth_access_token_refreshes_before_expiry(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv(
        "OSTIARI_WORKLOAD_TOKEN_URL",
        "https://identity.example/oauth2/token",
    )
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_ID", "gateway-a")
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_SECRET", "client-secret")
    now = 100.0
    calls = 0

    async def issue(_config):
        nonlocal calls
        calls += 1
        return f"token-{calls}", 10.0

    monkeypatch.setattr(workload_identity, "_request_oauth_token", issue)
    monkeypatch.setattr(workload_identity.time, "monotonic", lambda: now)

    assert await machine_headers() == {"Authorization": "Bearer token-1"}
    now = 109.0
    assert await machine_headers() == {"Authorization": "Bearer token-2"}


def test_production_oauth_configuration_is_valid(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv(
        "OSTIARI_WORKLOAD_TOKEN_URL",
        "https://identity.example/oauth2/token",
    )
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_ID", "gateway-a")
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_SECRET", "client-secret")

    validate_production_credential()


def test_production_oauth_requires_https(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv(
        "OSTIARI_WORKLOAD_TOKEN_URL",
        "http://identity.example/oauth2/token",
    )
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_ID", "gateway-a")
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_SECRET", "client-secret")

    with pytest.raises(WorkloadCredentialError, match="HTTPS"):
        validate_production_credential()


def test_production_rejects_ambiguous_credential_sources(
    tmp_path,
    monkeypatch,
):
    token_file = tmp_path / "token"
    token_file.write_text("projected-token", encoding="utf-8")
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_WORKLOAD_TOKEN_FILE", str(token_file))
    monkeypatch.setenv(
        "OSTIARI_WORKLOAD_TOKEN_URL",
        "https://identity.example/oauth2/token",
    )
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_ID", "gateway-a")
    monkeypatch.setenv("OSTIARI_WORKLOAD_CLIENT_SECRET", "client-secret")

    with pytest.raises(WorkloadCredentialError, match="either"):
        validate_production_credential()
