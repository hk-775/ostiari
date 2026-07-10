"""OpenAIAdapter — OpenAI SDK function calling integration."""

from __future__ import annotations

import time
from typing import Any

from ostiari.exceptions import AdapterNotInstalledError

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]

from ostiari.adapters.protocol import AdapterContext


class OpenAIAdapter:
    """Adapter for OpenAI's function calling / tool_calls API."""

    def __init__(self, client: Any = None) -> None:
        if openai is None:
            raise AdapterNotInstalledError(
                adapter="OpenAIAdapter",
                install_command="pip install ostiari[openai]",
            )
        self._client = client

    @property
    def name(self) -> str:
        return "openai"

    def wrap_tool_call(self, tool: str, params: dict[str, Any]) -> AdapterContext:
        return AdapterContext(
            action=tool,
            params=params,
            framework_meta={"sdk": "openai"},
            start_time=time.monotonic(),
        )

    def on_result(self, context: AdapterContext, result: Any) -> None:
        pass

    def on_error(self, context: AdapterContext, error: Exception) -> None:
        pass

    def get_framework_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"adapter": "openai"}
        if self._client is not None:
            state["base_url"] = str(getattr(self._client, "base_url", ""))
        return state
