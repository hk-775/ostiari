"""Response cache with per-tenant, per-project TTL configuration."""

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse


_StorageKey = tuple[str | None, str]


class _ScopedCacheKey(str):
    """String-compatible cache key carrying its authoritative tenant scope."""

    __slots__ = ("_tenant_id",)

    def __new__(
        cls,
        digest: str,
        tenant_id: str | None,
    ) -> "_ScopedCacheKey":
        instance = super().__new__(cls, digest)
        instance._tenant_id = tenant_id
        return instance

    @property
    def tenant_id(self) -> str | None:
        return self._tenant_id

    def __reduce_ex__(self, protocol: int) -> tuple[type, tuple[str, str | None]]:
        return type(self), (str(self), self.tenant_id)

    def __setattr__(self, name: str, value: object) -> None:
        if name != "_tenant_id" or hasattr(self, "_tenant_id"):
            raise AttributeError("cache key scope is immutable")
        super().__setattr__(name, value)


class CacheManager:
    """In-memory response cache with TTL-based expiration and LRU eviction."""

    MAX_ENTRIES = 10_000

    def __init__(self) -> None:
        self._cache: OrderedDict[_StorageKey, dict] = OrderedDict()

    async def get(
        self,
        cache_key: str,
        *,
        tenant_id: str | None = None,
    ) -> ChatCompletionResponse | None:
        """Look up a response only in the matching tenant namespace.

        A key returned by :meth:`compute_cache_key` carries its scope so
        existing gateway callers remain safe without passing ``tenant_id``
        twice. Raw string keys are legacy tenantless keys unless an explicit
        tenant is supplied.
        """
        storage_key = self._storage_key(cache_key, tenant_id)
        if storage_key is None:
            return None

        entry = self._cache.get(storage_key)
        if entry is None:
            return None
        if datetime.now(timezone.utc) >= entry["expires_at"]:
            del self._cache[storage_key]
            return None
        if entry["tenant_id"] != storage_key[0]:
            return None
        self._cache.move_to_end(storage_key)
        return entry["response"]

    async def put(
        self,
        cache_key: str,
        response: ChatCompletionResponse,
        ttl_seconds: int,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Store a response in one tenant namespace with TTL."""
        storage_key = self._storage_key(cache_key, tenant_id)
        if storage_key is None:
            raise ValueError("tenant_id does not match cache key scope")

        self._cache[storage_key] = {
            "response": response,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
            "tenant_id": storage_key[0],
        }
        self._cache.move_to_end(storage_key)
        while len(self._cache) > self.MAX_ENTRIES:
            self._cache.popitem(last=False)

    def compute_cache_key(
        self,
        request: ChatCompletionRequest,
        project_id: str,
        tenant_id: str | None = None,
    ) -> str:
        """Generate deterministic cache key from request parameters.

        Uses SHA-256 hash of (model, messages, system, temperature, max_tokens,
        top_p, stop, tools, tool_choice, tenant_id, project_id) serialized as
        sorted JSON. ``tenant_id`` remains optional for legacy single-tenant
        callers, but authenticated requests must provide it.

        ``tools``/``tool_choice`` are part of the key because they change the
        answer: the same prompt sent with a tool list can return a tool call and
        sent without one returns prose. Omitting them would serve a cached
        tool-free reply to a request that needed a tool call.
        """
        key_data = {
            "model": request.model,
            "messages": request.messages,
            "system": request.system,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "top_p": request.top_p,
            "stop": request.stop,
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "tenant_id": tenant_id,
            "project_id": project_id,
        }
        canonical = json.dumps(key_data, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return _ScopedCacheKey(digest, tenant_id)

    @staticmethod
    def _storage_key(
        cache_key: str,
        tenant_id: str | None,
    ) -> _StorageKey | None:
        """Resolve storage scope, failing closed on conflicting scope data."""
        if isinstance(cache_key, _ScopedCacheKey):
            if tenant_id is not None and tenant_id != cache_key.tenant_id:
                return None
            return cache_key.tenant_id, str(cache_key)
        return tenant_id, str(cache_key)
