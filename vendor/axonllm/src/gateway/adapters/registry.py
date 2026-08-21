"""Adapter registry for looking up provider adapters by name."""

from collections.abc import Callable

from src.gateway.adapters.base import ProviderAdapter


class AdapterRegistry:
    """Maintains a mapping of provider names to ProviderAdapter instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self._factories: dict[str, Callable[[], ProviderAdapter]] = {}

    def register(self, provider_name: str, adapter: ProviderAdapter) -> None:
        """Register an adapter for a provider name."""
        self._adapters[provider_name] = adapter
        self._factories.pop(provider_name, None)

    def register_lazy(
        self,
        provider_name: str,
        factory: Callable[[], ProviderAdapter],
    ) -> None:
        """Register an adapter factory without importing its implementation."""
        self._factories[provider_name] = factory
        self._adapters.pop(provider_name, None)

    def get(self, provider_name: str) -> ProviderAdapter:
        """Look up an adapter by provider name.

        Raises:
            KeyError: If no adapter is registered for the given provider name.
        """
        adapter = self._adapters.get(provider_name)
        if adapter is not None:
            return adapter
        factory = self._factories.get(provider_name)
        if factory is not None:
            adapter = factory()
            self._adapters[provider_name] = adapter
            return adapter
        available = sorted({*self._adapters, *self._factories})
        raise KeyError(
            f"No adapter registered for provider '{provider_name}'. "
            f"Available providers: {available}"
        )
