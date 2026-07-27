"""Redis-backed shared-state tests — fleet-wide rate limit / budget / wallets.

These prove the atomicity the feature exists for: limits that hold across
gateway *instances*, not just within one process. They need a reachable Redis;
set OSTIARI_TEST_REDIS_URL (e.g. redis://127.0.0.1:6379/0) to run them,
otherwise they skip. CI has no Redis, so they skip there by design — the
per-process fallback is covered by the rest of the suite (store=None).
"""

from __future__ import annotations

import asyncio
import os

import pytest

_URL = os.environ.get("OSTIARI_TEST_REDIS_URL", "")


def _store():
    """A SharedStore on a flushed test DB, or skip if Redis isn't reachable."""
    if not _URL:
        pytest.skip("set OSTIARI_TEST_REDIS_URL to run shared-store tests")
    import redis
    from ostiari_gateway.shared_store import SharedStore
    try:
        client = redis.Redis.from_url(_URL, socket_connect_timeout=2.0)
        client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip(f"Redis not reachable at {_URL}")
    client.flushdb()
    # unique prefix per test run so parallel runs don't collide
    return SharedStore(client, prefix=f"test-{os.getpid()}")


class TestSharedStorePrimitives:
    def test_rate_allow_enforces_limit(self):
        s = _store()
        results = [s.rate_allow("agent", 3, 60) for _ in range(5)]
        assert results == [True, True, True, False, False]

    def test_rate_limit_is_per_key(self):
        s = _store()
        assert s.rate_allow("a", 1, 60) is True
        assert s.rate_allow("a", 1, 60) is False   # a exhausted
        assert s.rate_allow("b", 1, 60) is True    # b independent

    def test_budget_reserve_blocks_over_limit(self):
        s = _store()
        assert s.budget_reserve("gw", 0.6, 1.0) is True
        assert s.budget_reserve("gw", 0.6, 1.0) is False   # 1.2 >= 1.0
        assert s.budget_spend("gw") == pytest.approx(0.6)

    def test_budget_adjust_reconciles_and_releases(self):
        s = _store()
        s.budget_reserve("gw", 0.6, 10.0)
        s.budget_adjust("gw", -0.3)                 # reconcile estimate→actual
        assert s.budget_spend("gw") == pytest.approx(0.3)

    def test_wallet_debit_atomic_and_signed(self):
        s = _store()
        s.upsert_wallet("w", {"balance_usdc": 1.0, "spent_today_usdc": 0.0,
                              "status": "active", "daily_limit_usdc": None,
                              "per_call_limit_usdc": None})
        ok, _, bal = s.wallet_debit("w", 0.6)
        assert ok and bal == pytest.approx(0.4)
        ok, reason, _ = s.wallet_debit("w", 0.6)
        assert not ok and "insufficient" in reason
        # negative can never credit
        ok, reason, bal = s.wallet_debit("w", -100.0)
        assert not ok and bal == pytest.approx(0.4)

    def test_wallet_missing_fails_closed(self):
        s = _store()
        ok, reason, _ = s.wallet_debit("ghost", 0.01)
        assert not ok and "no wallet" in reason

    def test_concurrent_debits_never_double_spend(self):
        import threading
        s = _store()
        s.upsert_wallet("hot", {"balance_usdc": 1.0, "spent_today_usdc": 0.0,
                                "status": "active", "daily_limit_usdc": None,
                                "per_call_limit_usdc": None})
        wins = []
        def hit():
            ok, _, _ = s.wallet_debit("hot", 0.6)
            if ok:
                wins.append(1)
        ts = [threading.Thread(target=hit) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(wins) == 1                        # only one $0.60 fits in $1.00
        assert float(s.wallet_get("hot")["balance"]) >= 0   # never negative


class TestWiredEnforcementIsFleetWide:
    """The enforcers wired to the store share state across INSTANCES."""

    def test_two_quota_instances_share_one_budget(self):
        s = _store()
        from ostiari_gateway.quota_enforcer import QuotaEnforcer
        q1, q2 = QuotaEnforcer(), QuotaEnforcer()
        for q in (q1, q2):
            q.attach_shared_store(s)
            q.configure({"budget_limit_usd": 1.0, "budget_key": "shared"})
        d1 = q1.check(model="m", estimated_cost=0.6, reserve=True)
        d2 = q2.check(model="m", estimated_cost=0.6, reserve=True)
        assert d1.allowed is True
        assert d2.allowed is False                   # different instance, same budget
        assert q2.get_status()["spend_scope"] == "fleet"

    def test_reconcile_estimate_to_actual_across_instance(self):
        s = _store()
        from ostiari_gateway.quota_enforcer import QuotaEnforcer
        q = QuotaEnforcer()
        q.attach_shared_store(s)
        q.configure({"budget_limit_usd": 10.0, "budget_key": "k"})
        d = q.check(model="m", estimated_cost=0.6, reserve=True)
        q.record_spend(0.3, reservation_id=d.reservation_id)
        assert q.get_status()["current_spend"] == pytest.approx(0.3)

    def test_release_returns_reservation_to_fleet_budget(self):
        s = _store()
        from ostiari_gateway.quota_enforcer import QuotaEnforcer
        q = QuotaEnforcer()
        q.attach_shared_store(s)
        q.configure({"budget_limit_usd": 1.0, "budget_key": "k"})
        d = q.check(model="m", estimated_cost=0.6, reserve=True)
        q.release_reservation(d.reservation_id)
        assert q.get_status()["current_spend"] == pytest.approx(0.0)

    def test_two_payment_instances_cannot_double_spend(self):
        s = _store()
        from ostiari_gateway.payments.gate import PaymentGate
        from ostiari_gateway.payments.models import Quote

        async def run():
            g1, g2 = PaymentGate(), PaymentGate()
            for g in (g1, g2):
                g.configure({"mode": "passthrough",
                             "wallets": [{"agent_id": "a", "balance_usdc": 1.0}]})
                g.attach_shared_store(s)
            q = Quote(action="p", amount_usdc=0.6, source="tool_402")
            results = await asyncio.gather(
                g1.settle_402(agent_id="a", action="p", quote=q),
                g2.settle_402(agent_id="a", action="p", quote=q),
                g1.settle_402(agent_id="a", action="p", quote=q),
            )
            return sum(1 for r in results if r.settled)

        settled = asyncio.run(run())
        assert settled == 1
        assert float(s.wallet_get("a")["balance"]) >= 0
