"""Unit tests for StrandsAdapter."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from ostiari.exceptions import AdapterNotInstalledError


class TestStrandsAdapter:
    def test_guarded_import_missing(self):
        with patch.dict(sys.modules, {"strands": None}):
            import importlib

            import ostiari.adapters.strands as mod

            importlib.reload(mod)
            with pytest.raises(AdapterNotInstalledError) as exc_info:
                mod.StrandsAdapter()
            assert "ostiari[strands]" in str(exc_info.value)

    def test_wrap_tool_call(self):
        with patch.dict(sys.modules, {"strands": MagicMock()}):
            import importlib

            import ostiari.adapters.strands as mod

            importlib.reload(mod)
            adapter = mod.StrandsAdapter()

            ctx = adapter.wrap_tool_call("send_email", {"to": "user@test.com"})
            assert ctx.action == "send_email"
            assert ctx.params == {"to": "user@test.com"}
            assert ctx.framework_meta["sdk"] == "strands"

    def test_name_property(self):
        with patch.dict(sys.modules, {"strands": MagicMock()}):
            import importlib

            import ostiari.adapters.strands as mod

            importlib.reload(mod)
            adapter = mod.StrandsAdapter()
            assert adapter.name == "strands"

    def test_get_framework_state_with_agent(self):
        with patch.dict(sys.modules, {"strands": MagicMock()}):
            import importlib

            import ostiari.adapters.strands as mod

            importlib.reload(mod)

            class FakeAgent:
                pass

            adapter = mod.StrandsAdapter(agent=FakeAgent())
            state = adapter.get_framework_state()
            assert state["agent_type"] == "FakeAgent"
