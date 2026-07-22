"""Tests for DoS guards: body-size limit + per-caller rate limiting."""

from __future__ import annotations

import pytest
from ostiari_gateway.models import ModulesConfig, SidecarConfig
from starlette.testclient import TestClient


def _app() -> TestClient:
    from ostiari_gateway.server import create_app
    return TestClient(create_app(initial_config=SidecarConfig(
        sidecar_id="dos-test", modules=ModulesConfig())))


class TestBodySizeLimit:
    def test_oversized_body_rejected_413(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_MAX_BODY_BYTES", "1000")
        c = _app()
        big = {"x": "A" * 5000}
        r = c.post("/tool/anything", json=big)
        assert r.status_code == 413
        assert "body exceeds" in r.json()["detail"]

    def test_normal_body_passes(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_MAX_BODY_BYTES", "1000000")
        c = _app()
        # small unknown tool -> 404 (not 413): body limit didn't trip
        r = c.post("/tool/unknown_tool", json={"q": "hi"}, headers={"X-Agent-Id": "a"})
        assert r.status_code != 413


class TestRateLimit:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", raising=False)
        c = _app()
        # many requests, no 429 when disabled
        codes = [c.get("/health").status_code for _ in range(20)]
        assert 429 not in codes

    def test_enforced_when_set(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", "3")
        c = _app()
        codes = [c.get("/health", headers={"X-Agent-Id": "spammer"}).status_code
                 for _ in range(6)]
        assert codes.count(200) == 3      # first 3 allowed
        assert codes.count(429) == 3      # rest limited

    def test_per_agent_isolation(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", "2")
        c = _app()
        # agent A exhausts its budget
        for _ in range(3):
            c.get("/health", headers={"X-Agent-Id": "A"})
        # agent B still gets its own budget
        assert c.get("/health", headers={"X-Agent-Id": "B"}).status_code == 200
