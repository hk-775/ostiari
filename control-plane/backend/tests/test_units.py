"""Unit tests for standalone control-plane modules: budget_reset scheduler,
rate_limiter, redis_client fallback, and persistence."""

from datetime import datetime, timezone

import pytest

from control_plane import persistence, redis_client
from control_plane.budget_reset import BudgetResetScheduler
from control_plane.rate_limiter import RateLimiter, _InMemoryBucket

pytestmark = pytest.mark.anyio


# ─── Budget reset scheduler ──────────────────────────────────────────────────

class TestBudgetResetScheduler:
    def test_manual_has_no_next_reset(self):
        s = BudgetResetScheduler()
        s.configure("manual")
        assert s.schedule == "manual"
        assert s.next_reset is None

    def test_compute_next_reset_daily_is_future_midnight(self):
        s = BudgetResetScheduler()
        s.schedule = "daily"
        nxt = s._compute_next_reset()
        now = datetime.now(timezone.utc)
        assert nxt > now
        assert (nxt.hour, nxt.minute, nxt.second) == (0, 0, 0)

    def test_compute_next_reset_weekly_is_monday(self):
        s = BudgetResetScheduler()
        s.schedule = "weekly"
        nxt = s._compute_next_reset()
        assert nxt.weekday() == 0  # Monday
        assert nxt > datetime.now(timezone.utc)

    def test_compute_next_reset_monthly_is_first_of_month(self):
        s = BudgetResetScheduler()
        s.schedule = "monthly"
        nxt = s._compute_next_reset()
        assert nxt.day == 1
        assert nxt > datetime.now(timezone.utc)

    def test_stop_is_safe_when_not_running(self):
        s = BudgetResetScheduler()
        s.stop()  # no task started — must not raise


# ─── In-memory rate limiter bucket ───────────────────────────────────────────

class TestInMemoryBucket:
    def test_allows_up_to_limit_then_blocks(self):
        b = _InMemoryBucket()
        allowed_count = 0
        for _ in range(5):
            ok, _rem = b.check("k", limit_rpm=3)
            allowed_count += ok
        assert allowed_count == 3  # 4th and 5th blocked

    def test_remaining_decrements(self):
        b = _InMemoryBucket()
        ok1, rem1 = b.check("k2", 10)
        ok2, rem2 = b.check("k2", 10)
        assert ok1 and ok2
        assert rem1 == 9 and rem2 == 8

    def test_keys_are_independent(self):
        b = _InMemoryBucket()
        b.check("a", 1)
        ok, _ = b.check("b", 1)  # different key still allowed
        assert ok


# ─── RateLimiter falls back to in-memory when Redis absent ───────────────────

class TestRateLimiterFallback:
    async def test_uses_memory_when_no_redis(self, monkeypatch):
        # get_redis returns None in the test env (REDIS_URL unset) -> in-memory path
        async def _no_redis():
            return None
        monkeypatch.setattr("control_plane.rate_limiter.get_redis", _no_redis)

        rl = RateLimiter(prefix="test")
        results = [await rl.check("client-x", limit_rpm=2) for _ in range(3)]
        allowed = [ok for ok, _ in results]
        assert allowed == [True, True, False]


# ─── Redis client graceful absence ───────────────────────────────────────────

class TestRedisClient:
    async def test_get_redis_none_without_url(self, monkeypatch):
        monkeypatch.setattr(redis_client, "REDIS_URL", None)
        monkeypatch.setattr(redis_client, "_redis", None)
        assert await redis_client.get_redis() is None


# ─── Persistence (JSON state round-trip) ─────────────────────────────────────

class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "state.json")
        persistence.save_state({"quotas": [{"name": "q"}], "n": 1})
        loaded = persistence.load_state()
        assert loaded["n"] == 1
        assert loaded["quotas"][0]["name"] == "q"

    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "nope.json")
        assert persistence.load_state() == {}

    def test_load_corrupt_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json")
        monkeypatch.setattr(persistence, "STATE_FILE", f)
        assert persistence.load_state() == {}


# ─── Provider api-key encryption ─────────────────────────────────────────────

class TestProviderEncryption:
    def test_roundtrip_with_configured_key(self):
        from control_plane.routers import providers
        secret = "sk-configured-abc123"
        assert providers._decrypt(providers._encrypt(secret)) == secret

    def test_roundtrip_with_transient_key(self, monkeypatch):
        # Simulate OSTIARI_ENCRYPTION_KEY unset: the cached cipher must stay
        # stable so encrypt/decrypt use the SAME transient key (regression test
        # for a bug where a fresh key was minted on every call).
        from control_plane.routers import providers
        monkeypatch.setattr(providers, "_ENCRYPTION_KEY", "")
        monkeypatch.setattr(providers, "_fernet", None)
        token = providers._encrypt("sk-transient-xyz")
        assert providers._decrypt(token) == "sk-transient-xyz"

    def test_empty_string_passthrough(self):
        from control_plane.routers import providers
        assert providers._encrypt("") == ""
        assert providers._decrypt("") == ""
