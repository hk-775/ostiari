"""Redis-backed shared state for fleet-wide enforcement.

Rate-limit windows, budget spend, and payment wallets are per-process by
default — so a horizontally-scaled gateway fleet multiplies the effective
limit by the replica count (N instances ⇒ N× `rate_limit_rpm`/`budget_limit_usd`,
and wallet balances diverge per pod). This module backs those counters with
Redis so the limits hold across the whole fleet.

Design:
- **Optional & fail-safe.** `get_shared_store()` returns None unless a Redis
  endpoint is configured AND reachable at startup. Every caller keeps its
  existing in-process path when the store is None, so dev/demo behavior is
  unchanged and a Redis outage degrades to per-process limits rather than an
  outage of the gateway itself.
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

import logging
import os
import time

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


class SharedStore:
    """Thin atomic wrapper over Redis for cross-replica enforcement counters."""

    def __init__(self, client, prefix: str = "ostiari") -> None:
        self._r = client
        self._prefix = prefix
        # register_script gives us EVALSHA with automatic fallback to EVAL.
        self._rate = client.register_script(_RATE_LUA)
        self._reserve = client.register_script(_BUDGET_RESERVE_LUA)
        self._debit = client.register_script(_WALLET_DEBIT_LUA)

    def _k(self, *parts: str) -> str:
        return ":".join((self._prefix, *parts))

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
            return bool(res)
        except Exception as e:  # noqa: BLE001 — never let Redis break the request
            log.warning("shared rate_allow failed (allowing): %s", e)
            return True

    # ── budget ────────────────────────────────────────────────────────────────
    def budget_reserve(self, key: str, amount: float, limit: float) -> bool:
        """Atomically reserve `amount` against a fleet-wide budget `limit`."""
        try:
            return bool(self._reserve(keys=[self._k("budget", key)], args=[amount, limit]))
        except Exception as e:  # noqa: BLE001
            log.warning("shared budget_reserve failed (allowing): %s", e)
            return True

    def budget_adjust(self, key: str, delta: float) -> None:
        """Add `delta` to shared spend (reconcile estimate→actual, or release)."""
        if not delta:
            return
        try:
            self._r.incrbyfloat(self._k("budget", key), delta)
        except Exception as e:  # noqa: BLE001
            log.warning("shared budget_adjust failed: %s", e)

    def budget_spend(self, key: str) -> float:
        try:
            v = self._r.get(self._k("budget", key))
            return float(v) if v is not None else 0.0
        except Exception as e:  # noqa: BLE001
            log.warning("shared budget_spend read failed: %s", e)
            return 0.0

    def budget_reset(self, key: str) -> None:
        try:
            self._r.delete(self._k("budget", key))
        except Exception as e:  # noqa: BLE001
            log.warning("shared budget_reset failed: %s", e)

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
        except Exception as e:  # noqa: BLE001
            log.warning("shared upsert_wallet failed: %s", e)

    def wallet_debit(self, agent_id: str, amount: float) -> tuple[bool, str, float]:
        """Atomically check-and-debit a wallet. Returns (ok, reason, new_balance)."""
        try:
            ok, reason, bal = self._debit(keys=[self._k("wallet", agent_id)], args=[amount])
            reason = reason.decode() if isinstance(reason, bytes) else reason
            bal = bal.decode() if isinstance(bal, bytes) else bal
            return bool(ok), reason, float(bal)
        except Exception as e:  # noqa: BLE001
            # Fail CLOSED on a wallet error — do not settle a charge we can't verify.
            log.warning("shared wallet_debit failed (denying): %s", e)
            return False, "shared store error", 0.0

    def wallet_get(self, agent_id: str) -> dict | None:
        try:
            h = self._r.hgetall(self._k("wallet", agent_id))
            if not h:
                return None
            d = {(k.decode() if isinstance(k, bytes) else k):
                 (v.decode() if isinstance(v, bytes) else v) for k, v in h.items()}
            return d
        except Exception as e:  # noqa: BLE001
            log.warning("shared wallet_get failed: %s", e)
            return None


# ── module singleton ─────────────────────────────────────────────────────────

_store: SharedStore | None = None
_resolved = False


def _redis_url() -> str:
    url = os.environ.get("OSTIARI_REDIS_URL", "").strip()
    if url:
        return url
    endpoint = os.environ.get("REDIS_ENDPOINT", "").strip()
    if not endpoint:
        return ""
    port = os.environ.get("REDIS_PORT", "6379").strip() or "6379"
    return f"redis://{endpoint}:{port}/0"


def get_shared_store() -> SharedStore | None:
    """Return the shared store, or None when Redis is unconfigured/unreachable.

    Resolved once and cached. A connection is verified with PING at startup; if
    it fails, we log and return None so the gateway runs with per-process limits
    rather than failing to start.
    """
    global _store, _resolved
    if _resolved:
        return _store
    _resolved = True
    url = _redis_url()
    if not url:
        return None
    try:
        import redis  # redis-py (sync); redis[hiredis] declared in gateway deps
        client = redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
        client.ping()
        prefix = os.environ.get("OSTIARI_REDIS_PREFIX", "ostiari").strip() or "ostiari"
        _store = SharedStore(client, prefix=prefix)
        log.info("Shared state backed by Redis at %s (prefix=%s)", url, prefix)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "Redis configured (%s) but unreachable — falling back to per-process "
            "limits: %s", url, e,
        )
        _store = None
    return _store


def reset_shared_store() -> None:
    """Test hook — clear the cached singleton so env changes take effect."""
    global _store, _resolved
    _store = None
    _resolved = False
