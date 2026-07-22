"""Tests for the CORS config helper (wildcard-with-credentials avoidance)."""

import importlib


class TestCorsConfig:
    def test_default_wildcard_without_credentials(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_CORS_ORIGINS", raising=False)
        app_mod = importlib.import_module("control_plane.app")
        cfg = app_mod._cors_config()
        assert cfg["allow_origins"] == ["*"]
        # the unsafe combo (wildcard + credentials) must NOT happen
        assert cfg["allow_credentials"] is False

    def test_explicit_origins_with_credentials(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_CORS_ORIGINS", "https://app.example.com, https://admin.example.com")
        app_mod = importlib.import_module("control_plane.app")
        cfg = app_mod._cors_config()
        assert cfg["allow_origins"] == ["https://app.example.com", "https://admin.example.com"]
        assert cfg["allow_credentials"] is True
