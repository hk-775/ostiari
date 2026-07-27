"""Module registry — discovers and activates pluggable modules."""

import logging
from typing import Any, Protocol

from fastapi import FastAPI

log = logging.getLogger("ostiari.sidecar.modules")


class SidecarModule(Protocol):
    """Protocol that all sidecar modules must implement."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def register(self, app: FastAPI, context: dict[str, Any]) -> None:
        """Register routes and startup logic on the FastAPI app."""
        ...

    def shutdown(self) -> None:
        """Clean up resources."""
        ...


class ModuleRegistry:
    """Manages activation and lifecycle of sidecar modules."""

    def __init__(self) -> None:
        self._available: dict[str, SidecarModule] = {}
        self._active: dict[str, SidecarModule] = {}

    def discover(self) -> None:
        """Discover built-in modules."""
        try:
            from ostiari_gateway.modules.llm_gateway import LLMGatewayModule

            self._available["llm_gateway"] = LLMGatewayModule()
        except ImportError:
            log.debug("LLM Gateway module not available")

    def activate(
        self, module_name: str, app: FastAPI, context: dict[str, Any]
    ) -> bool:
        """Activate a module by name."""
        module = self._available.get(module_name)
        if module is None:
            log.warning("Module %s not found in available modules", module_name)
            return False

        if module_name in self._active:
            log.debug("Module %s already active", module_name)
            return True

        module.register(app, context)
        self._active[module_name] = module
        log.info("Activated module: %s", module_name)
        return True

    def deactivate(self, module_name: str) -> bool:
        """Deactivate a module."""
        module = self._active.pop(module_name, None)
        if module is None:
            return False
        module.shutdown()
        log.info("Deactivated module: %s", module_name)
        return True

    def get(self, module_name: str) -> SidecarModule | None:
        """The active module instance, or None if it isn't active.

        Lets the server inspect a module's own state after activation (e.g.
        whether the LLM gateway got AxonLLM embedded) without reaching through
        the private dicts.
        """
        return self._active.get(module_name)

    def get_active(self) -> list[str]:
        return list(self._active.keys())

    def get_available(self) -> list[dict[str, str]]:
        return [
            {"name": m.name, "description": m.description}
            for m in self._available.values()
        ]

    def shutdown_all(self) -> None:
        for name in list(self._active.keys()):
            self.deactivate(name)
