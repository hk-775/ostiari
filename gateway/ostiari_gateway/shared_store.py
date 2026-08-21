"""Redis-backed shared state for fleet-wide enforcement.

Rate-limit windows, budget spend, and payment wallets are per-process by
default — so a horizontally-scaled gateway fleet multiplies the effective
limit by the replica count (N instances ⇒ N× `rate_limit_rpm`/`budget_limit_usd`,
and wallet balances diverge per pod). This module backs those counters with
Redis so the limits hold across the whole fleet.

Design:
- **Optional in development, mandatory in production.** Development keeps the
  existing per-process fallback. Production requires Redis at startup and marks
  readiness unavailable if the shared store later fails.
- **Atomic.** Each operation is a single Lua script (one round-trip), so the
  check-and-mutate is atomic across all clients — no TOCTOU between replicas.
- **Sync client.** The scripts are tiny (sub-ms on same-host/VPC Redis) and are
  called from both sync (`QuotaEnforcer.check`) and async (rate-limit
  middleware, payment gate) sites; a sync client avoids threading `await`
  through those call chains. The brief event-loop touch on the async paths is
  an accepted trade for fleet-correct limits.

Configuration (first match wins):
  OSTIARI_REDIS_URL   e.g. redis://:pass@host:6379/0
  REDIS_ENDPOINT [+ REDIS_PORT, default 6379]   (the deploy-manifest convention)
Key namespace is prefixed by OSTIARI_REDIS_PREFIX (default "ostiari") so several
gateways/tenants can share one Redis.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("ostiari.sidecar.shared_store")

# ── Lua scripts (atomic; one round-trip each) ────────────────────────────────

# Sliding-window rate limit. Prune entries older than the window, count what's
# left, and admit (adding this hit) only if under the limit.
#   KEYS[1] = window key
#   ARGV[1] = now (ms)  ARGV[2] = window (ms)  ARGV[3] = limit  ARGV[4] = member
# Returns 1 (allowed) or 0 (limited).
_RATE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, tonumber(ARGV[1]) - tonumber(ARGV[2]))
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

# Budget reserve: admit (and immediately add `amount` to the shared spend) only
# if the projected total stays under the limit. This reserves optimistically —
# record_spend later reconciles estimate→actual, release subtracts it back.
#   KEYS[1] = spend key   ARGV[1] = amount   ARGV[2] = limit
# Returns 1 (reserved) or 0 (would exceed).
_BUDGET_RESERVE_LUA = """
local spend = tonumber(redis.call('GET', KEYS[1]) or '0')
if (spend + tonumber(ARGV[1])) >= tonumber(ARGV[2]) then return 0 end
redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
return 1
"""

# Atomic wallet check-and-debit. Wallet is a hash: balance, spent_today,
# status, and optional daily_limit / per_call_limit (empty string = unset).
#   KEYS[1] = wallet key   ARGV[1] = amount
# Returns {ok, reason, new_balance_str}.
_WALLET_DEBIT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {0, 'no wallet', '0'} end
local amt = tonumber(ARGV[1])
if amt < 0 then return {0, 'negative charge', redis.call('HGET', KEYS[1], 'balance') or '0'} end
local status = redis.call('HGET', KEYS[1], 'status') or 'active'
if status ~= 'active' then return {0, 'wallet ' .. status, redis.call('HGET', KEYS[1], 'balance') or '0'} end
local bal = tonumber(redis.call('HGET', KEYS[1], 'balance') or '0')
local spent = tonumber(redis.call('HGET', KEYS[1], 'spent_today') or '0')
local pcl = redis.call('HGET', KEYS[1], 'per_call_limit')
if pcl and pcl ~= '' and amt > tonumber(pcl) then return {0, 'per-call limit', tostring(bal)} end
if bal < amt then return {0, 'insufficient balance', tostring(bal)} end
local dl = redis.call('HGET', KEYS[1], 'daily_limit')
if dl and dl ~= '' and (spent + amt) > tonumber(dl) then return {0, 'daily limit', tostring(bal)} end
local nb = bal - amt
redis.call('HSET', KEYS[1], 'balance', tostring(nb), 'spent_today', tostring(spent + amt))
if dl and dl ~= '' and (spent + amt) >= tonumber(dl) then redis.call('HSET', KEYS[1], 'status', 'paused') end
return {1, '', tostring(nb)}
"""


def shared_store_required() -> bool:
    value = os.environ.get("OSTIARI_REQUIRE_REDIS", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    return os.environ.get("OSTIARI_ENV", "").strip().lower() in {
        "production",
        "prod",
    }


class SharedStore:
    """Thin atomic wrapper over Redis for cross-replica enforcement counters."""

    def __init__(
        self,
        client,
        prefix: str = "ostiari",
        *,
        required: bool = False,
    ) -> None:
        self._r = client
        self._prefix = prefix
        self._required = required
        self._healthy = True
        self._last_error = ""
        # register_script gives us EVALSHA with automatic fallback to EVAL.
        self._rate = client.register_script(_RATE_LUA)
        self._reserve = client.register_script(_BUDGET_RESERVE_LUA)
        self._debit = client.register_script(_WALLET_DEBIT_LUA)

    def _ok(self) -> None:
        self._healthy = True
        self._last_error = ""

    def _failed(self, operation: str, error: Exception) -> None:
        self._healthy = False
        self._last_error = f"{operation}: {error}"
        log.warning("shared %s failed: %s", operation, error)

    def status(self, *, check: bool = False) -> dict[str, object]:
        """Return secret-free health metadata, optionally probing Redis now."""
        if check:
            try:
                self._r.ping()
                self._ok()
            except Exception as exc:  # noqa: BLE001
                self._failed("ping", exc)
        return {
            "configured": True,
            "required": self._required,
            "healthy": self._healthy,
            "last_error": self._last_error,
        }

    def _k(self, *parts: str) -> str:
        return ":".join((self._prefix, *parts))

    @property
    def required(self) -> bool:
        return self._required

    # ── durable event outbox ────────────────────────────────────────────────
    def outbox_enqueue(
        self,
        stream: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Append one immutable event to a Redis Stream."""
        try:
            encoded = json.dumps(
                payload,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._r.xadd(
                self._k("outbox", stream),
                {"event_id": event_id, "payload": encoded},
            )
            self._ok()
            return True
        except Exception as e:  # noqa: BLE001
            self._failed(f"outbox_enqueue[{stream}]", e)
            return False

    def outbox_read(
        self,
        stream: str,
        *,
        count: int = 100,
    ) -> list[tuple[str, str, dict[str, Any]]] | None:
        """Read oldest events without acknowledging them."""
        try:
            rows = self._r.xrange(
                self._k("outbox", stream),
                min="-",
                max="+",
                count=count,
            )
            events: list[tuple[str, str, dict[str, Any]]] = []
            for raw_receipt, raw_fields in rows:
                receipt = (
                    raw_receipt.decode()
                    if isinstance(raw_receipt, bytes)
                    else str(raw_receipt)
                )
                fields = {
                    key.decode() if isinstance(key, bytes) else str(key):
                    value.decode() if isinstance(value, bytes) else str(value)
                    for key, value in raw_fields.items()
                }
                event_id = fields.get("event_id", "")
                payload = json.loads(fields.get("payload", ""))
                if not event_id or not isinstance(payload, dict):
                    raise ValueError(f"invalid event at Redis Stream id {receipt}")
                events.append((receipt, event_id, payload))
            self._ok()
            return events
        except Exception as e:  # noqa: BLE001
            self._failed(f"outbox_read[{stream}]", e)
            return None

    def outbox_ack(self, stream: str, receipts: list[str]) -> bool:
        """Delete confirmed Redis Stream entries."""
        if not receipts:
            return True
        try:
            self._r.xdel(self._k("outbox", stream), *receipts)
            self._ok()
            return True
        except Exception as e:  # noqa: BLE001
            self._failed(f"outbox_ack[{stream}]", e)
            return False

    def outbox_depth(self, stream: str) -> int | None:
        try:
            depth = int(self._r.xlen(self._k("outbox", stream)))
            self._ok()
            return depth
        except Exception as e:  # noqa: BLE001
            self._failed(f"outbox_depth[{stream}]", e)
            return None

    # ── rate limiting ────────────────────────────────────────────────────────
    def rate_allow(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        """True if `key` may proceed under a sliding `limit`/`window_s`."""
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}-{os.urandom(4).hex()}"  # unique even within one ms
        try:
            res = self._rate(
                keys=[self._k("rate", key)],
                args=[now_ms, int(window_s * 1000), limit, member],
            )
            self._ok()
            return bool(res)
        except Exception as e:  # noqa: BLE001 — never let Redis break the request
            self._failed("rate_allow", e)
            return not self._required

    # ── budget ────────────────────────────────────────────────────────────────
    def budget_reserve(self, key: str, amount: float, limit: float) -> bool:
        """Atomically reserve `amount` against a fleet-wide budget `limit`."""
        try:
            result = bool(
                self._reserve(keys=[self._k("budget", key)], args=[amount, limit])
            )
            self._ok()
            return result
        except Exception as e:  # noqa: BLE001
            self._failed("budget_reserve", e)
            return not self._required

    def budget_adjust(self, key: str, delta: float) -> bool:
        """Add `delta` to shared spend (reconcile estimate→actual, or release)."""
        if not delta:
            return True
        try:
            self._r.incrbyfloat(self._k("budget", key), delta)
            self._ok()
            return True
        except Exception as e:  # noqa: BLE001
            self._failed("budget_adjust", e)
            return False

    def budget_spend(self, key: str) -> float | None:
        try:
            v = self._r.get(self._k("budget", key))
            self._ok()
            return float(v) if v is not None else 0.0
        except Exception as e:  # noqa: BLE001
            self._failed("budget_spend", e)
            return None if self._required else 0.0

    def budget_reset(self, key: str) -> bool:
        try:
            self._r.delete(self._k("budget", key))
            self._ok()
            return True
        except Exception as e:  # noqa: BLE001
            self._failed("budget_reset", e)
            return False

    # ── wallets ────────────────────────────────────────────────────────────────
    def upsert_wallet(self, agent_id: str, fields: dict) -> None:
        """Write a wallet's state to the shared store (idempotent overwrite)."""
        mapping = {
            "balance": str(fields.get("balance_usdc", 0.0)),
            "spent_today": str(fields.get("spent_today_usdc", 0.0)),
            "status": fields.get("status", "active"),
            "daily_limit": "" if fields.get("daily_limit_usdc") is None else str(fields["daily_limit_usdc"]),
            "per_call_limit": "" if fields.get("per_call_limit_usdc") is None else str(fields["per_call_limit_usdc"]),
        }
        try:
            self._r.hset(self._k("wallet", agent_id), mapping=mapping)
            self._ok()
        except Exception as e:  # noqa: BLE001
            self._failed("upsert_wallet", e)

    def wallet_debit(self, agent_id: str, amount: float) -> tuple[bool, str, float]:
        """Atomically check-and-debit a wallet. Returns (ok, reason, new_balance)."""
        try:
            ok, reason, bal = self._debit(keys=[self._k("wallet", agent_id)], args=[amount])
            reason = reason.decode() if isinstance(reason, bytes) else reason
            bal = bal.decode() if isinstance(bal, bytes) else bal
            self._ok()
            return bool(ok), reason, float(bal)
        except Exception as e:  # noqa: BLE001
            # Fail CLOSED on a wallet error — do not settle a charge we can't verify.
            self._failed("wallet_debit", e)
            return False, "shared store error", 0.0

    def wallet_get(self, agent_id: str) -> dict | None:
        try:
            h = self._r.hgetall(self._k("wallet", agent_id))
            if not h:
                self._ok()
                return None
            d = {(k.decode() if isinstance(k, bytes) else k):
                 (v.decode() if isinstance(v, bytes) else v) for k, v in h.items()}
            self._ok()
            return d
        except Exception as e:  # noqa: BLE001
            self._failed("wallet_get", e)
            return None


# ── module singleton ─────────────────────────────────────────────────────────

_store: SharedStore | None = None
_resolved = False
_resolve_error = ""


def _redis_url() -> str:
    url = os.environ.get("OSTIARI_REDIS_URL", "").strip()
    if url:
        return url
    endpoint = os.environ.get("REDIS_ENDPOINT", "").strip()
    if not endpoint:
        return ""
    port = os.environ.get("REDIS_PORT", "6379").strip() or "6379"
    return f"redis://{endpoint}:{port}/0"


def _safe_redis_url(url: str) -> str:
    """Redact Redis credentials before logging configuration errors."""
    parsed = urlsplit(url)
    if not parsed.netloc or "@" not in parsed.netloc:
        return url
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, f"***@{host}", parsed.path, parsed.query, ""))


def get_shared_store() -> SharedStore | None:
    """Return the shared store, or None when Redis is unconfigured/unreachable.

    Resolved once and cached. A connection is verified with PING at startup.
    Development falls back to per-process limits; production/required mode
    raises so a fleet cannot silently multiply enforcement limits.
    """
    global _store, _resolved, _resolve_error
    required = shared_store_required()
    if _resolved:
        if required and _store is None:
            raise RuntimeError(_resolve_error or "Redis shared state is unavailable")
        return _store

    url = _redis_url()
    if not url:
        _resolved = True
        _resolve_error = "Redis is required but OSTIARI_REDIS_URL/REDIS_ENDPOINT is unset"
        if required:
            raise RuntimeError(_resolve_error)
        return None
    try:
        import redis  # redis-py (sync); redis[hiredis] declared in gateway deps
        client = redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
        client.ping()
        prefix = os.environ.get("OSTIARI_REDIS_PREFIX", "ostiari").strip() or "ostiari"
        _store = SharedStore(client, prefix=prefix, required=required)
        _resolve_error = ""
        log.info(
            "Shared state backed by Redis at %s (prefix=%s)",
            _safe_redis_url(url),
            prefix,
        )
    except Exception as e:  # noqa: BLE001
        safe_url = _safe_redis_url(url)
        _resolve_error = f"Redis configured at {safe_url} but unavailable: {e}"
        _store = None
        if required:
            _resolved = True
            raise RuntimeError(_resolve_error) from e
        log.warning(
            "%s — falling back to per-process limits",
            _resolve_error,
        )
    _resolved = True
    return _store


def reset_shared_store() -> None:
    """Test hook — clear the cached singleton so env changes take effect."""
    global _store, _resolved, _resolve_error
    _store = None
    _resolved = False
    _resolve_error = ""
