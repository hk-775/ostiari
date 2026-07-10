"""Tool registry — maps tool names to callables for proxy execution."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("ostiari.proxy")


class ToolRegistry:
    """Registry of tool implementations that the proxy can execute."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> Callable[..., Any] | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    @classmethod
    def from_config(cls, config_path: str | Path) -> ToolRegistry:
        """Load tools from a YAML config file.

        Format:
            tools:
              send_email:
                module: mytools.email
                function: send
              db_query:
                module: mytools.database
                function: execute_query
        """
        registry = cls()
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Tool config not found: {path}")

        data = yaml.safe_load(path.read_text())
        tools = data.get("tools", {})

        for name, spec in tools.items():
            module_name = spec["module"]
            func_name = spec["function"]
            try:
                module = importlib.import_module(module_name)
                fn = getattr(module, func_name)
                registry.register(name, fn)
                log.info("Registered tool: %s → %s.%s", name, module_name, func_name)
            except (ImportError, AttributeError) as e:
                log.warning("Failed to load tool %s: %s", name, e)

        return registry
