"""Unit tests for the FrameworkAdapter protocol and validation."""

from __future__ import annotations

import time

import pytest

from ostiari.adapters.protocol import AdapterContext, FrameworkAdapter, validate_adapter
from ostiari.exceptions import AdapterValidationError


class ValidAdapter:
    @property
    def name(self) -> str:
        return "valid"

    def wrap_tool_call(self, tool, params):
        return AdapterContext(
            action=tool, params=params, framework_meta={}, start_time=time.monotonic()
        )

    def on_result(self, context, result):
        pass

    def on_error(self, context, error):
        pass

    def get_framework_state(self):
        return {}


class MissingMethodAdapter:
    @property
    def name(self) -> str:
        return "incomplete"

    def wrap_tool_call(self, tool, params):
        return AdapterContext(action=tool, params=params, framework_meta={}, start_time=0)


class TestAdapterContext:
    def test_creation(self):
        ctx = AdapterContext(action="test", params={"k": "v"}, framework_meta={}, start_time=1.0)
        assert ctx.action == "test"
        assert ctx.params == {"k": "v"}
        assert ctx.start_time == 1.0

    def test_immutable(self):
        ctx = AdapterContext(action="test", params={}, framework_meta={}, start_time=0)
        with pytest.raises(AttributeError):
            ctx.action = "changed"  # type: ignore[misc]


class TestProtocolCompliance:
    def test_valid_adapter_passes(self):
        adapter = ValidAdapter()
        validate_adapter(adapter)
        assert isinstance(adapter, FrameworkAdapter)

    def test_missing_methods_raises(self):
        adapter = MissingMethodAdapter()
        with pytest.raises(AdapterValidationError) as exc_info:
            validate_adapter(adapter)
        assert "on_result" in exc_info.value.missing
        assert "on_error" in exc_info.value.missing
        assert "get_framework_state" in exc_info.value.missing

    def test_none_raises(self):
        with pytest.raises(AdapterValidationError):
            validate_adapter(object())

    def test_error_message_format(self):
        with pytest.raises(AdapterValidationError) as exc_info:
            validate_adapter(MissingMethodAdapter())
        msg = str(exc_info.value)
        assert "incomplete" in msg
        assert "FrameworkAdapter" in msg
