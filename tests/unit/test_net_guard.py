"""Tests for the SSRF guard (validate_public_url)."""

from __future__ import annotations

import pytest

from ostiari.net_guard import SSRFError, validate_public_url


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        "ostiari.net_guard.socket.getaddrinfo",
        lambda host, port: [(None, None, None, "", ("8.8.8.8", 0))],
    )


class TestAlwaysBlocked:
    def test_metadata_blocked_in_dev(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        with pytest.raises(SSRFError, match="link-local|metadata"):
            validate_public_url("http://169.254.169.254/latest/meta-data/iam/")

    def test_metadata_blocked_in_prod(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        with pytest.raises(SSRFError, match="link-local|metadata"):
            validate_public_url("http://169.254.169.254/")

    def test_metadata_not_allowlist_escapable(self, monkeypatch):
        # Even if someone allowlists it, metadata stays blocked.
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_SSRF_ALLOW", "169.254.0.0/16")
        with pytest.raises(SSRFError, match="link-local|metadata"):
            validate_public_url("http://169.254.169.254/")

    def test_non_http_scheme_blocked(self):
        with pytest.raises(SSRFError, match="scheme"):
            validate_public_url("file:///etc/passwd")
        with pytest.raises(SSRFError, match="scheme"):
            validate_public_url("gopher://internal/")

    def test_no_host(self):
        with pytest.raises(SSRFError):
            validate_public_url("http://")

    def test_url_userinfo_blocked(self):
        with pytest.raises(SSRFError, match="userinfo"):
            validate_public_url("https://user:password@example.com/api")


class TestDevPermissive:
    def test_localhost_allowed_in_dev(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        # the demo flow: import-openapi http://localhost:9300
        assert validate_public_url("http://localhost:9300/openapi.json")
        assert validate_public_url("http://127.0.0.1:8400/spec")

    def test_public_allowed_in_dev(self, monkeypatch, public_dns):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        assert validate_public_url("https://api.github.com/openapi.json")


class TestProductionStrict:
    def test_localhost_blocked_in_prod(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.delenv("OSTIARI_SSRF_ALLOW", raising=False)
        with pytest.raises(SSRFError, match="private|internal"):
            validate_public_url("http://localhost:9300/spec")

    def test_private_ip_blocked_in_prod(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.delenv("OSTIARI_SSRF_ALLOW", raising=False)
        with pytest.raises(SSRFError, match="private|internal"):
            validate_public_url("http://10.0.0.5/spec")

    def test_allowlist_permits_specific_cidr_in_prod(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_SSRF_ALLOW", "10.0.0.0/8")
        assert validate_public_url("http://10.0.0.5/spec")

    def test_public_still_allowed_in_prod(self, monkeypatch, public_dns):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        assert validate_public_url("https://api.github.com/openapi.json")
