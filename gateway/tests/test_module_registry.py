"""Lifecycle contracts for pluggable gateway modules."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from ostiari_gateway.modules.registry import ModuleRegistry


class _Module:
    name = "test"
    description = "test module"

    def __init__(self) -> None:
        self.registered = False
        self.closed = False

    def register(self, app: FastAPI, context: dict[str, Any]) -> None:
        self.registered = True

    async def shutdown(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_deactivate_awaits_module_shutdown() -> None:
    registry = ModuleRegistry()
    module = _Module()
    registry._available[module.name] = module

    assert registry.activate(module.name, FastAPI(), {})
    assert await registry.deactivate(module.name)
    assert module.closed
    assert registry.get_active() == []


@pytest.mark.asyncio
async def test_shutdown_all_closes_every_active_module() -> None:
    registry = ModuleRegistry()
    first = _Module()
    second = _Module()
    second.name = "second"
    registry._available = {first.name: first, second.name: second}

    assert registry.activate(first.name, FastAPI(), {})
    assert registry.activate(second.name, FastAPI(), {})
    await registry.shutdown_all()

    assert first.closed
    assert second.closed
    assert registry.get_active() == []
