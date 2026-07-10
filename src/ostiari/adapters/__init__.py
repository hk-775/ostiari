"""Framework adapters — auto-detection and convenience imports."""

from __future__ import annotations

import importlib.util
from typing import Any

from ostiari.adapters.protocol import AdapterContext, FrameworkAdapter, validate_adapter


def available_adapters() -> dict[str, bool]:
    """Check which framework SDKs are installed (without importing them)."""
    return {
        "claude": importlib.util.find_spec("anthropic") is not None,
        "openai": importlib.util.find_spec("openai") is not None,
        "bedrock": importlib.util.find_spec("boto3") is not None,
        "strands": importlib.util.find_spec("strands") is not None,
    }


def __getattr__(name: str) -> Any:
    if name == "ClaudeAdapter":
        from ostiari.adapters.claude import ClaudeAdapter

        return ClaudeAdapter
    if name == "OpenAIAdapter":
        from ostiari.adapters.openai import OpenAIAdapter

        return OpenAIAdapter
    if name == "BedrockAdapter":
        from ostiari.adapters.bedrock import BedrockAdapter

        return BedrockAdapter
    if name == "StrandsAdapter":
        from ostiari.adapters.strands import StrandsAdapter

        return StrandsAdapter
    raise AttributeError(f"module 'ostiari.adapters' has no attribute {name!r}")


__all__ = [
    "AdapterContext",
    "FrameworkAdapter",
    "available_adapters",
    "validate_adapter",
    "ClaudeAdapter",
    "OpenAIAdapter",
    "BedrockAdapter",
    "StrandsAdapter",
]
