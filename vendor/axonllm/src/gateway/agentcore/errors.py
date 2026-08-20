"""Public, sanitized errors raised by the AgentCore adapter."""

from __future__ import annotations


class AgentCoreAdapterError(Exception):
    """An invocation failure safe to expose through the runtime boundary."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
