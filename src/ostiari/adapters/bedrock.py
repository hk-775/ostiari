"""BedrockAdapter — Amazon Bedrock Agents action group integration."""

from __future__ import annotations

import time
from typing import Any

from ostiari.exceptions import AdapterNotInstalledError

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]

from ostiari.adapters.protocol import AdapterContext


class BedrockAdapter:
    """Adapter for Amazon Bedrock Agents action groups."""

    def __init__(self, region: str | None = None, agent_id: str | None = None) -> None:
        if boto3 is None:
            raise AdapterNotInstalledError(
                adapter="BedrockAdapter",
                install_command="pip install ostiari[bedrock]",
            )
        self._region = region
        self._agent_id = agent_id

    @property
    def name(self) -> str:
        return "bedrock"

    def wrap_tool_call(self, tool: str, params: dict[str, Any]) -> AdapterContext:
        action_group = params.pop("__action_group__", "default")
        action = f"{action_group}.{tool}"
        return AdapterContext(
            action=action,
            params=params,
            framework_meta={"sdk": "bedrock", "action_group": action_group},
            start_time=time.monotonic(),
        )

    def on_result(self, context: AdapterContext, result: Any) -> None:
        pass

    def on_error(self, context: AdapterContext, error: Exception) -> None:
        pass

    def get_framework_state(self) -> dict[str, Any]:
        return {
            "adapter": "bedrock",
            "region": self._region,
            "agent_id": self._agent_id,
        }
