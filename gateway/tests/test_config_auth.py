"""Tests for gateway /config auth + credential redaction (assessment finding #2)."""

from __future__ import annotations

from ostiari_gateway.models import ModulesConfig, SidecarConfig
from starlette.testclient import TestClient


def _app() -> TestClient:
    from ostiari_gateway.server import create_app
    return TestClient(create_app(initial_config=SidecarConfig(
        sidecar_id="cfg-test", modules=ModulesConfig(llm_gateway=True),
        llm={"default_model": "claude-sonnet-4-6",
             "credentials": {"anthropic": "DUMMY-TEST-CRED-not-a-real-key", "bedrock_region": "us-east-1"}})))


class TestCredentialRedaction:
    def test_get_config_redacts_provider_keys(self):
        c = _app()
        cfg = c.get("/config").json()
        creds = cfg.get("llm", {}).get("credentials", {})
        assert creds.get("anthropic") == "***REDACTED***"
        # non-secret config preserved
        assert creds.get("bedrock_region") == "us-east-1"
        # the real secret never appears anywhere in the response body
        assert "DUMMY-TEST-CRED-not-a-real-key" not in c.get("/config").text


class TestConfigAuth:
    def test_config_open_when_key_unset(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_CONFIG_ADMIN_KEY", raising=False)
        c = _app()
        assert c.get("/config/mode").status_code == 200
        assert c.post("/config/mode", json={"mode": "enforce"}).status_code == 200

    def test_config_requires_key_when_set(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_CONFIG_ADMIN_KEY", "s3cret")
        c = _app()
        # no key -> 401
        assert c.get("/config").status_code == 401
        assert c.post("/config/mode", json={"mode": "shadow"}).status_code == 401
        # wrong key -> 401
        assert c.get("/config", headers={"X-Config-Admin-Key": "nope"}).status_code == 401
        # correct key -> allowed
        assert c.get("/config", headers={"X-Config-Admin-Key": "s3cret"}).status_code == 200
        # correct key via Bearer -> allowed
        assert c.post("/config/mode", json={"mode": "enforce"},
                      headers={"Authorization": "Bearer s3cret"}).status_code == 200

    def test_non_config_paths_unaffected(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_CONFIG_ADMIN_KEY", "s3cret")
        c = _app()
        # /health and /tools are not under /config -> not gated
        assert c.get("/health").status_code == 200
        assert c.get("/tools").status_code == 200
