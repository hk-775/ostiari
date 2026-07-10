"""FrameworkAdapter protocol and supporting types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ostiari.exceptions import AdapterValidationError


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Context produced by an adapter's pre-call hook, consumed by post/error hooks."""

    action: str
    params: dict[str, Any]
    framework_meta: dict[str, Any]
    start_time: float


@runtime_checkable
class FrameworkAdapter(Protocol):
    """Protocol for framework-specific integration adapters."""

    @property
    def name(self) -> str: ...

    def wrap_tool_call(self, tool: str, params: dict[str, Any]) -> AdapterContext: ...

    def on_result(self, context: AdapterContext, result: Any) -> None: ...

    def on_error(self, context: AdapterContext, error: Exception) -> None: ...

    def get_framework_state(self) -> dict[str, Any]: ...


_REQUIRED_METHODS = ("wrap_tool_call", "on_result", "on_error", "get_framework_state")
_REQUIRED_PROPERTIES = ("name",)


def validate_adapter(adapter: Any) -> None:
    """Validate that an object implements the FrameworkAdapter protocol."""
    missing: list[str] = []
    for method in _REQUIRED_METHODS:
        attr = getattr(adapter, method, None)
        if attr is None or not callable(attr):
            missing.append(method)
    for prop in _REQUIRED_PROPERTIES:
        if not hasattr(adapter, prop):
            missing.append(prop)
    if missing:
        raise AdapterValidationError(
            adapter=getattr(adapter, "name", repr(adapter)),
            missing=missing,
        )
