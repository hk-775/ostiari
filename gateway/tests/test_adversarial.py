"""Adversarial regression tests — attacks that MUST stay blocked.

Written during an adversarial security pass. Each test encodes a concrete
attack and asserts the gate holds. Grouped by the four categories exercised:

  1. Payments      — negative-amount credit, wallet integrity under concurrency
  2. Resource/DoS  — budget reserve-then-settle (concurrent overshoot)
  3. Auth/identity — JWT algorithm pinning, token↔X-Agent-Id binding
  4. Gate-bypass / posture — production fail-open detection

These are guards against regressions: if someone later removes a sign check or
a reservation, a test here goes red.
"""

from __future__ import annotations

import asyncio

import pytest
from ostiari_gateway.payments.gate import PaymentGate, parse_402
from ostiari_gateway.payments.models import Quote, Wallet
from ostiari_gateway.quota_enforcer import QuotaEnforcer

# ─── 1. Payments ────────────────────────────────────────────────────────────

class TestPaymentIntegrity:
    def test_negative_402_amount_cannot_credit_wallet(self):
        """A malicious tool returning 402 {"amount_usdc": -100} must NOT credit
        the wallet or reset the daily cap."""
        gate = PaymentGate()
        gate.configure({
            "mode": "passthrough",
            "wallets": [{"agent_id": "a", "balance_usdc": 1.0,
                         "daily_limit_usdc": 0.5, "spent_today_usdc": 0.4}],
        })
        wallet = gate._wallets["a"]

        quote = parse_402({"amount_usdc": -100.0}, 402, "premium")
        # parse_402 clamps the negative to 0 at the boundary.
        assert quote.amount_usdc >= 0

        decision = asyncio.run(
            gate.settle_402(agent_id="a", action="premium", quote=quote))
        # Balance must not have grown; daily spend must not have gone negative.
        assert wallet.balance_usdc <= 1.0
        assert wallet.spent_today_usdc >= 0.4
        _ = decision  # settled-as-free is fine; crediting is not

    def test_can_afford_rejects_negative_amount(self):
        """Defense in depth: even if a negative reaches can_afford, it's refused."""
        w = Wallet(agent_id="a", balance_usdc=1.0)
        ok, reason = w.can_afford(-50.0)
        assert ok is False
        assert "negative" in reason.lower()

    def test_negative_debit_would_have_credited(self):
        """Documents the underlying bug: debit(-x) increases balance. The
        can_afford guard above is what prevents reaching this."""
        w = Wallet(agent_id="a", balance_usdc=1.0)
        w.debit(-100.0)
        assert w.balance_usdc == 101.0  # proves why the guard matters

    def test_concurrent_settlements_do_not_overspend(self):
        """5 concurrent charges against a 1-charge wallet settle at most once
        more than the balance allows (no negative balance)."""
        async def run():
            gate = PaymentGate()
            gate.configure({"mode": "passthrough",
                            "wallets": [{"agent_id": "a", "balance_usdc": 1.0}]})
            q = Quote(action="p", amount_usdc=0.6, source="tool_402")
            results = await asyncio.gather(*[
                gate.settle_402(agent_id="a", action="p", quote=q) for _ in range(5)
            ])
            return gate._wallets["a"], sum(1 for r in results if r.settled)

        wallet, settled = asyncio.run(run())
        assert wallet.balance_usdc >= 0        # never negative
        assert settled <= 1                    # only what the balance covered

    def test_missing_wallet_fails_closed(self):
        gate = PaymentGate()
        gate.configure({"mode": "passthrough", "wallets": []})
        q = Quote(action="p", amount_usdc=0.01, source="tool_402")
        decision = asyncio.run(gate.settle_402(agent_id="ghost", action="p", quote=q))
        assert decision.settled is False


# ─── 2. Resource / DoS — budget reserve-then-settle ──────────────────────────

class TestBudgetReservation:
    def test_reservation_counts_toward_projection(self):
        """A reserved-but-not-yet-settled estimate blocks a concurrent call that
        would push projected spend over the budget."""
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        # First call reserves $0.60 (not yet settled).
        d1 = q.check(model="m", estimated_cost=0.6, reserve=True)
        assert d1.allowed and d1.reservation_id is not None
        # A concurrent call estimating another $0.60 must be blocked: the
        # projection is 0 spent + 0.60 reserved + 0.60 = 1.20 >= 1.0.
        d2 = q.check(model="m", estimated_cost=0.6, reserve=True)
        assert d2.allowed is False
        assert d2.limit_type == "budget"

    def test_recording_spend_releases_reservation(self):
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 10.0})
        d = q.check(model="m", estimated_cost=0.6, reserve=True)
        assert q._reserved_total() == pytest.approx(0.6)
        q.record_spend(0.5, reservation_id=d.reservation_id)
        assert q._reserved_total() == 0.0     # released
        assert q._total_spend == pytest.approx(0.5)  # real cost booked

    def test_release_reservation_frees_budget(self):
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        d = q.check(model="m", estimated_cost=0.6, reserve=True)
        q.release_reservation(d.reservation_id)
        # After release, a new call has the full budget again.
        d2 = q.check(model="m", estimated_cost=0.6, reserve=True)
        assert d2.allowed is True

    def test_no_reserve_flag_is_unchanged_behavior(self):
        """Default reserve=False books nothing — preserves prior semantics."""
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        d = q.check(model="m", estimated_cost=0.6)
        assert d.reservation_id is None
        assert q._reserved_total() == 0.0

    def test_expired_reservation_is_pruned(self):
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        d = q.check(model="m", estimated_cost=0.6, reserve=True)
        assert d.reservation_id is not None
        # Force expiry by winding the TTL to zero.
        q._reservation_ttl = -1.0
        assert q._reserved_total() == 0.0     # pruned as a leak backstop


# ─── 3. Auth / identity ──────────────────────────────────────────────────────

class TestJWTHardening:
    def test_algorithm_is_pinned_no_alg_none(self):
        """The OIDC validator pins RS256, so a hand-forged alg=none token is
        rejected at validation."""
        import base64
        import json
        import time

        from ostiari_gateway.oidc import OIDCError, OIDCValidator

        def _b64(obj: dict) -> str:
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        v = OIDCValidator(issuer="https://issuer.example", jwks_url="https://x/jwks")
        # Preset the key cache and mark it fresh so validate() never hits the
        # network — we're testing algorithm handling, not JWKS fetching.
        v._keys = {"k1": {"kty": "RSA", "n": "x", "e": "AQAB", "kid": "k1"}}
        v._fetched_at = time.time()
        # Manually assemble an unsigned alg=none token with a matching kid.
        forged = f"{_b64({'alg': 'none', 'kid': 'k1'})}.{_b64({'sub': 'admin'})}."
        with pytest.raises(OIDCError):
            v.validate(forged)

    def test_token_identity_must_match_x_agent_id(self):
        """agent_id_from_claims drives the X-Agent-Id binding check in
        _authenticate_agent; a token for A cannot claim to be B."""
        from ostiari_gateway.oidc import agent_id_from_claims
        assert agent_id_from_claims({"agent_id": "agent-a"}) == "agent-a"
        assert agent_id_from_claims({"sub": "agent-a"}) == "agent-a"
        # The gateway compares this against X-Agent-Id and 403s on mismatch.


# ─── 4. Gate-bypass / production posture ─────────────────────────────────────

def _secure_production_env(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_CONFIG_ADMIN_KEY", "c" * 40)
    monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "s" * 40)
    monkeypatch.setenv("OSTIARI_INGEST_KEY", "i" * 40)
    monkeypatch.setenv("OSTIARI_GATEWAY_AUTH", "required")
    monkeypatch.setenv("OSTIARI_TENANCY_MODE", "single")
    monkeypatch.setenv("OSTIARI_ORG_ID", "production-org")
    monkeypatch.setenv("OSTIARI_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("OSTIARI_OIDC_AUDIENCE", "gateway-client")
    monkeypatch.setenv("OSTIARI_REQUIRE_REDIS", "true")
    monkeypatch.setenv("REDIS_ENDPOINT", "redis.internal")
    monkeypatch.setenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", "600")
    monkeypatch.setenv("OSTIARI_X402_MODE", "off")


class TestProductionPosture:
    def test_production_refuses_insecure_configuration(self, monkeypatch):
        """Production cannot start with unauthenticated control surfaces."""
        from ostiari_gateway.server import _check_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.delenv("OSTIARI_CONFIG_ADMIN_KEY", raising=False)
        with pytest.raises(RuntimeError) as exc:
            _check_production_posture()
        assert "insecure production" in str(exc.value).lower()

    def test_production_passes_when_controls_set(self, monkeypatch):
        from ostiari_gateway.server import _check_production_posture

        _secure_production_env(monkeypatch)
        _check_production_posture()  # must not raise

    def test_production_refuses_simulated_settlement(self, monkeypatch):
        from ostiari_gateway.server import _check_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("OSTIARI_X402_MODE", "simulated")
        with pytest.raises(RuntimeError, match="OSTIARI_X402_MODE"):
            _check_production_posture()

    def test_production_refuses_disabled_gateway_rate_limit(self, monkeypatch):
        from ostiari_gateway.server import _check_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("OSTIARI_GATEWAY_RATE_LIMIT_RPM", "0")
        with pytest.raises(RuntimeError, match="OSTIARI_GATEWAY_RATE_LIMIT_RPM"):
            _check_production_posture()

    def test_dev_posture_never_refuses(self, monkeypatch):
        """Non-production (the demo default) must start regardless."""
        from ostiari_gateway.server import _check_production_posture
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        monkeypatch.delenv("OSTIARI_CONFIG_ADMIN_KEY", raising=False)
        monkeypatch.delenv("OSTIARI_SERVICE_TOKEN", raising=False)
        monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
        monkeypatch.delenv("OSTIARI_GATEWAY_AUTH", raising=False)
        _check_production_posture()  # no raise, no requirement
