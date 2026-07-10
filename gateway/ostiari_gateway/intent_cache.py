"""Intent Cache — caches LLM tool plans to avoid redundant LLM calls.

When an agent sends the same intent (goal) within a session, the gateway
reuses the cached tool plan from the previous LLM response instead of
making another LLM call. Saves cost and latency.

Cache key: hash(session_id + normalized_intent)
Cache value: list of tool calls the LLM returned
Eviction: TTL-based (configurable) + per-session + max entries
"""

import hashlib
import logging
import time
from typing import Any

log = logging.getLogger("ostiari.sidecar.intent_cache")


class CachedPlan:
    """A cached tool plan from a previous LLM response."""

    def __init__(
        self,
        tool_calls: list[dict[str, Any]],
        model_used: str,
        created_at: float,
        ttl_seconds: float,
        is_template: bool = False,
    ) -> None:
        self.tool_calls = tool_calls
        self.model_used = model_used
        self.created_at = created_at
        self.ttl_seconds = ttl_seconds
        self.is_template = is_template
        self.hit_count = 0

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.ttl_seconds

    def resolve_with_variables(self, variables: dict[str, str]) -> list[dict[str, Any]]:
        """Substitute variables into the cached tool plan's arguments.

        Replaces {var_name} placeholders in argument values with actual values.
        """
        if not variables or not self.is_template:
            return self.tool_calls

        import json as _json
        resolved = []
        for tc in self.tool_calls:
            # Deep substitute variables in arguments
            args_str = _json.dumps(tc.get("arguments", {}))
            for var_name, var_value in variables.items():
                args_str = args_str.replace(f"{{{var_name}}}", var_value)
            resolved.append({
                **tc,
                "arguments": _json.loads(args_str),
            })
        return resolved


class IntentCache:
    """Caches intent → tool plan mappings per session.

    Lookup: O(1) hash map
    Eviction: TTL (default 5 minutes) + max entries (default 100)
    """

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 100) -> None:
        self._cache: dict[str, CachedPlan] = {}
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def _make_key(self, agent_id: str, session_id: str, intent: str) -> str:
        """Create cache key from agent + session + normalized intent.

        Strict isolation: cache is per-agent, per-session. No sharing across agents.
        """
        normalized = intent.strip().lower()
        raw = f"{agent_id}:{session_id}:{normalized}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, agent_id: str, session_id: str, intent: str) -> CachedPlan | None:
        """Look up a cached tool plan for this intent in this agent's session.

        Returns None on miss or expired entry. Never returns another agent's cached plan.
        """
        if not agent_id or not session_id or not intent:
            self._misses += 1
            return None

        key = self._make_key(agent_id, session_id, intent)
        plan = self._cache.get(key)

        if plan is None:
            self._misses += 1
            return None

        if plan.is_expired:
            del self._cache[key]
            self._misses += 1
            log.debug("Intent cache expired: agent=%s session=%s", agent_id, session_id)
            return None

        plan.hit_count += 1
        self._hits += 1
        log.info(
            "Intent cache HIT: agent=%s session=%s, tools=%d, model=%s (hit #%d)",
            agent_id, session_id, len(plan.tool_calls), plan.model_used, plan.hit_count,
        )
        return plan

    def put(self, agent_id: str, session_id: str, intent: str, tool_calls: list[dict[str, Any]], model_used: str, is_template: bool = False) -> None:
        """Cache a tool plan for this intent within this agent's session."""
        if not agent_id or not session_id or not intent or not tool_calls:
            return

        # Evict if at capacity
        if len(self._cache) >= self._max_entries:
            self._evict_oldest()

        key = self._make_key(agent_id, session_id, intent)
        self._cache[key] = CachedPlan(
            tool_calls=tool_calls,
            model_used=model_used,
            created_at=time.monotonic(),
            ttl_seconds=self._ttl_seconds,
            is_template=is_template,
        )
        log.debug(
            "Intent cached: agent=%s session=%s, tools=%d, model=%s, template=%s",
            agent_id, session_id, len(tool_calls), model_used, is_template,
        )

    def invalidate_session(self, session_id: str) -> int:
        """Remove all cached plans for a session."""
        keys_to_remove = [
            k for k, v in self._cache.items()
            # Can't reverse the hash, so we remove all (simple approach)
            # In production, maintain a session → keys index
        ]
        # For now, just clear expired entries
        removed = self._prune_expired()
        return removed

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        self._prune_expired()
        total = self._hits + self._misses
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total * 100, 1) if total > 0 else 0,
            "ttl_seconds": self._ttl_seconds,
            "max_entries": self._max_entries,
        }

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()

    def _evict_oldest(self) -> None:
        """Remove the oldest entry."""
        if not self._cache:
            return
        oldest_key = min(self._cache, key=lambda k: self._cache[k].created_at)
        del self._cache[oldest_key]

    def _prune_expired(self) -> int:
        """Remove all expired entries."""
        expired = [k for k, v in self._cache.items() if v.is_expired]
        for k in expired:
            del self._cache[k]
        return len(expired)
