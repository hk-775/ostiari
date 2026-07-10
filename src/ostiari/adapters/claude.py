"""ClaudeAdapter — Anthropic SDK tool use integration."""

from __future__ import annotations

import time
from typing import Any

from ostiari.exceptions import AdapterNotInstalledError

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from ostiari.adapters.protocol import AdapterContext


class ClaudeAdapter:
    """Adapter for Anthropic's Claude SDK tool_use blocks."""

    def __init__(self, client: Any = None) -> None:
        if anthropic is None:
            raise AdapterNotInstalledError(
                adapter="ClaudeAdapter",
                install_command="pip install ostiari[claude]",
            )
        self._client = client

    @property
    def name(self) -> str:
        return "claude"

    def wrap_tool_call(self, tool: str, params: dict[str, Any]) -> AdapterContext:
        return AdapterContext(
            action=tool,
            params=params,
            framework_meta={"sdk": "anthropic"},
            start_time=time.monotonic(),
        )

    def on_result(self, context: AdapterContext, result: Any) -> None:
        pass

    def on_error(self, context: AdapterContext, error: Exception) -> None:
        pass

    def get_framework_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"adapter": "claude"}
        if self._client is not None:
            state["max_retries"] = getattr(self._client, "max_retries", None)
        return state
