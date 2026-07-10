"""StrandsAdapter — AWS Strands Agents SDK tool integration."""

from __future__ import annotations

import time
from typing import Any

from ostiari.exceptions import AdapterNotInstalledError

try:
    import strands  # noqa: F401
except ImportError:
    strands = None  # type: ignore[assignment]

from ostiari.adapters.protocol import AdapterContext


class StrandsAdapter:
    """Adapter for AWS Strands Agents SDK tools."""

    def __init__(self, agent: Any = None) -> None:
        if strands is None:
            raise AdapterNotInstalledError(
                adapter="StrandsAdapter",
                install_command="pip install ostiari[strands]",
            )
        self._agent = agent

    @property
    def name(self) -> str:
        return "strands"

    def wrap_tool_call(self, tool: str, params: dict[str, Any]) -> AdapterContext:
        return AdapterContext(
            action=tool,
            params=params,
            framework_meta={"sdk": "strands"},
            start_time=time.monotonic(),
        )

    def on_result(self, context: AdapterContext, result: Any) -> None:
        pass

    def on_error(self, context: AdapterContext, error: Exception) -> None:
        pass

    def get_framework_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"adapter": "strands"}
        if self._agent is not None:
            state["agent_type"] = type(self._agent).__name__
        return state
