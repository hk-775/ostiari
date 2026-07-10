"""Unit tests for OpenAIAdapter."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from ostiari.exceptions import AdapterNotInstalledError


class TestOpenAIAdapter:
    def test_guarded_import_missing(self):
        with patch.dict(sys.modules, {"openai": None}):
            import importlib

            import ostiari.adapters.openai as mod

            importlib.reload(mod)
            with pytest.raises(AdapterNotInstalledError) as exc_info:
                mod.OpenAIAdapter()
            assert "ostiari[openai]" in str(exc_info.value)

    def test_wrap_tool_call(self):
        with patch.dict(sys.modules, {"openai": MagicMock()}):
            import importlib

            import ostiari.adapters.openai as mod

            importlib.reload(mod)
            adapter = mod.OpenAIAdapter()

            ctx = adapter.wrap_tool_call("search_db", {"query": "test"})
            assert ctx.action == "search_db"
            assert ctx.params == {"query": "test"}
            assert ctx.framework_meta["sdk"] == "openai"

    def test_name_property(self):
        with patch.dict(sys.modules, {"openai": MagicMock()}):
            import importlib

            import ostiari.adapters.openai as mod

            importlib.reload(mod)
            adapter = mod.OpenAIAdapter()
            assert adapter.name == "openai"

    def test_get_framework_state_with_client(self):
        with patch.dict(sys.modules, {"openai": MagicMock()}):
            import importlib

            import ostiari.adapters.openai as mod

            importlib.reload(mod)
            mock_client = MagicMock()
            mock_client.base_url = "https://api.openai.com"
            adapter = mod.OpenAIAdapter(client=mock_client)
            state = adapter.get_framework_state()
            assert state["adapter"] == "openai"
            assert "api.openai.com" in state["base_url"]
