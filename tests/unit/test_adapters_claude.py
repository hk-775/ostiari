"""Unit tests for ClaudeAdapter."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from ostiari.exceptions import AdapterNotInstalledError


class TestClaudeAdapter:
    def test_guarded_import_missing(self):
        with patch.dict(sys.modules, {"anthropic": None}):
            import importlib

            import ostiari.adapters.claude as mod

            importlib.reload(mod)
            with pytest.raises(AdapterNotInstalledError) as exc_info:
                mod.ClaudeAdapter()
            assert "ostiari[claude]" in str(exc_info.value)

    def test_wrap_tool_call(self):
        with patch.dict(sys.modules, {"anthropic": MagicMock()}):
            import importlib

            import ostiari.adapters.claude as mod

            importlib.reload(mod)
            adapter = mod.ClaudeAdapter()

            ctx = adapter.wrap_tool_call("get_weather", {"city": "Seattle"})
            assert ctx.action == "get_weather"
            assert ctx.params == {"city": "Seattle"}
            assert ctx.framework_meta["sdk"] == "anthropic"
            assert ctx.start_time > 0

    def test_name_property(self):
        with patch.dict(sys.modules, {"anthropic": MagicMock()}):
            import importlib

            import ostiari.adapters.claude as mod

            importlib.reload(mod)
            adapter = mod.ClaudeAdapter()
            assert adapter.name == "claude"

    def test_on_result_no_raise(self):
        with patch.dict(sys.modules, {"anthropic": MagicMock()}):
            import importlib

            import ostiari.adapters.claude as mod

            importlib.reload(mod)
            adapter = mod.ClaudeAdapter()
            ctx = adapter.wrap_tool_call("test", {})
            adapter.on_result(ctx, {"data": "value"})

    def test_on_error_no_raise(self):
        with patch.dict(sys.modules, {"anthropic": MagicMock()}):
            import importlib

            import ostiari.adapters.claude as mod

            importlib.reload(mod)
            adapter = mod.ClaudeAdapter()
            ctx = adapter.wrap_tool_call("test", {})
            adapter.on_error(ctx, RuntimeError("test error"))

    def test_get_framework_state(self):
        with patch.dict(sys.modules, {"anthropic": MagicMock()}):
            import importlib

            import ostiari.adapters.claude as mod

            importlib.reload(mod)
            mock_client = MagicMock()
            mock_client.max_retries = 3
            adapter = mod.ClaudeAdapter(client=mock_client)
            state = adapter.get_framework_state()
            assert state["adapter"] == "claude"
            assert state["max_retries"] == 3
