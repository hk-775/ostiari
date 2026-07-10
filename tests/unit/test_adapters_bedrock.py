"""Unit tests for BedrockAdapter."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from ostiari.exceptions import AdapterNotInstalledError


class TestBedrockAdapter:
    def test_guarded_import_missing(self):
        with patch.dict(sys.modules, {"boto3": None}):
            import importlib

            import ostiari.adapters.bedrock as mod

            importlib.reload(mod)
            with pytest.raises(AdapterNotInstalledError) as exc_info:
                mod.BedrockAdapter()
            assert "ostiari[bedrock]" in str(exc_info.value)

    def test_wrap_tool_call_with_action_group(self):
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            import importlib

            import ostiari.adapters.bedrock as mod

            importlib.reload(mod)
            adapter = mod.BedrockAdapter(region="us-east-1")

            ctx = adapter.wrap_tool_call("getOrder", {"id": "123", "__action_group__": "orders"})
            assert ctx.action == "orders.getOrder"
            assert ctx.params == {"id": "123"}
            assert ctx.framework_meta["action_group"] == "orders"

    def test_wrap_tool_call_default_group(self):
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            import importlib

            import ostiari.adapters.bedrock as mod

            importlib.reload(mod)
            adapter = mod.BedrockAdapter()

            ctx = adapter.wrap_tool_call("listItems", {"page": 1})
            assert ctx.action == "default.listItems"

    def test_name_property(self):
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            import importlib

            import ostiari.adapters.bedrock as mod

            importlib.reload(mod)
            adapter = mod.BedrockAdapter()
            assert adapter.name == "bedrock"

    def test_get_framework_state(self):
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            import importlib

            import ostiari.adapters.bedrock as mod

            importlib.reload(mod)
            adapter = mod.BedrockAdapter(region="eu-west-1", agent_id="agent-abc")
            state = adapter.get_framework_state()
            assert state["region"] == "eu-west-1"
            assert state["agent_id"] == "agent-abc"
