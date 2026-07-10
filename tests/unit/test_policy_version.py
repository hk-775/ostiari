"""Unit tests for PolicyVersion and reload_from_content."""

from __future__ import annotations

import hashlib

from ostiari.policy.engine import PolicyEngine


class TestPolicyVersion:
    def test_initial_version(self):
        engine = PolicyEngine()
        v = engine.current_version
        assert v.hash == "00000000"
        assert v.source == "none"
        assert v.rule_count == 0

    def test_reload_from_content_updates_version(self):
        engine = PolicyEngine()
        content = b"rules: []"
        expected_hash = hashlib.sha256(content).hexdigest()[:8]

        result = engine.reload_from_content(content, source="https://example.com/policy.yaml")
        assert result is True

        v = engine.current_version
        assert v.hash == expected_hash
        assert v.source == "https://example.com/policy.yaml"
        assert v.rule_count == 0

    def test_reload_from_content_with_rules(self):
        engine = PolicyEngine()
        content = b"""
rules:
  - type: block
    action: "*.delete"
    description: No deletes
    priority: 100
"""
        result = engine.reload_from_content(content, source="file:///etc/policy.yaml")
        assert result is True

        v = engine.current_version
        assert v.rule_count == 1
        assert v.source == "file:///etc/policy.yaml"

    def test_reload_invalid_content_keeps_old_version(self):
        engine = PolicyEngine()
        good_content = b"rules: []"
        engine.reload_from_content(good_content, source="test")
        old_version = engine.current_version

        bad_content = b"not: valid: yaml: {{{"
        result = engine.reload_from_content(bad_content, source="bad")
        assert result is False
        assert engine.current_version == old_version

    def test_hash_deterministic(self):
        content = b"rules:\n  - type: allow\n    action: safe.*\n    priority: 50"
        expected = hashlib.sha256(content).hexdigest()[:8]

        engine = PolicyEngine()
        engine.reload_from_content(content, source="test")
        assert engine.current_version.hash == expected

        engine2 = PolicyEngine()
        engine2.reload_from_content(content, source="test")
        assert engine2.current_version.hash == expected
