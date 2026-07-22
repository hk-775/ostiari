"""Tests for #4: fail-closed posture.

The guard's fail_open default is env-overridable; when fail-closed, an evaluator
exception must raise (block) rather than fabricate an allow.
"""

from __future__ import annotations

import pytest

from ostiari.models import OstiariConfig, _default_fail_open


class TestFailOpenDefault:
    def test_default_is_open(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_FAIL_OPEN", raising=False)
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        assert _default_fail_open() is True
        assert OstiariConfig().fail_open is True

    def test_explicit_false_is_closed(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_FAIL_OPEN", "false")
        assert _default_fail_open() is False

    def test_production_is_closed(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_FAIL_OPEN", raising=False)
        monkeypatch.setenv("OSTIARI_ENV", "production")
        assert _default_fail_open() is False

    def test_explicit_true_overrides_production(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_FAIL_OPEN", "true")
        assert _default_fail_open() is True


class TestGuardFailClosed:
    def test_fail_open_allows_on_evaluator_error(self):
        from ostiari import Guard
        from ostiari.exceptions import OstiariError

        g = Guard(config=OstiariConfig(fail_open=True))
        g.start()
        # Force an evaluator to blow up
        g._policy_engine.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        # fail-open: should NOT raise (degrades to allow)
        try:
            g.validate(action="x", params={}, context={"agent_id": "a"})
        except OstiariError:
            pytest.fail("fail_open=True should not raise on evaluator error")

    def test_fail_closed_raises_on_evaluator_error(self):
        from ostiari import Guard
        from ostiari.exceptions import OstiariError

        g = Guard(config=OstiariConfig(fail_open=False))
        g.start()
        g._policy_engine.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        with pytest.raises(OstiariError):
            g.validate(action="x", params={}, context={"agent_id": "a"})
