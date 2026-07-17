"""Tests for the x402 payment gate: wallets, pricing, settlement, and the
metered + passthrough flows through the sidecar proxy."""

import pytest
from ostiari_gateway.models import PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.payments import (
    PaymentGate as Gate,
)
from ostiari_gateway.payments import (
    Quote,
    SimulatedSettler,
    Wallet,
    parse_402,
)
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient

# ─── Wallet model (unit) ─────────────────────────────────────────────────────

class TestWallet:
    def test_can_afford_ok(self):
        w = Wallet(agent_id="a", balance_usdc=1.0)
        assert w.can_afford(0.5) == (True, "")

    def test_insufficient_balance(self):
        w = Wallet(agent_id="a", balance_usdc=0.001)
        ok, reason = w.can_afford(0.5)
        assert ok is False and "insufficient balance" in reason

    def test_per_call_limit(self):
        w = Wallet(agent_id="a", balance_usdc=100, per_call_limit_usdc=0.01)
        ok, reason = w.can_afford(0.5)
        assert ok is False and "per-call limit" in reason

    def test_daily_limit(self):
        w = Wallet(agent_id="a", balance_usdc=100, daily_limit_usdc=1.0, spent_today_usdc=0.9)
        ok, reason = w.can_afford(0.5)
        assert ok is False and "daily limit" in reason

    def test_paused_wallet_rejected(self):
        w = Wallet(agent_id="a", balance_usdc=100, status="paused")
        ok, reason = w.can_afford(0.001)
        assert ok is False and "paused" in reason

    def test_debit_advances_spend(self):
        w = Wallet(agent_id="a", balance_usdc=1.0)
        w.debit(0.3)
        assert w.balance_usdc == pytest.approx(0.7)
        assert w.spent_today_usdc == pytest.approx(0.3)

    def test_debit_auto_pauses_at_daily_limit(self):
        w = Wallet(agent_id="a", balance_usdc=10, daily_limit_usdc=0.5)
        w.debit(0.5)
        assert w.status == "paused"


# ─── SimulatedSettler (unit) ─────────────────────────────────────────────────

class TestSimulatedSettler:
    async def test_settles_and_debits(self):
        s = SimulatedSettler()
        w = Wallet(agent_id="a", balance_usdc=1.0)
        r = await s.settle(quote=Quote(action="x", amount_usdc=0.2), wallet=w)
        assert r.settled is True
        assert r.tx_hash.startswith("sim-")
        assert w.balance_usdc == pytest.approx(0.8)

    async def test_rejects_when_unaffordable(self):
        s = SimulatedSettler()
        w = Wallet(agent_id="a", balance_usdc=0.01)
        r = await s.settle(quote=Quote(action="x", amount_usdc=0.5), wallet=w)
        assert r.settled is False
        assert "insufficient balance" in r.reason
        assert w.balance_usdc == pytest.approx(0.01)  # untouched


# ─── Pricing (unit) ──────────────────────────────────────────────────────────

class TestPricing:
    def test_default_free(self):
        g = Gate()
        g.configure({"mode": "metered", "default": 0.0})
        assert g.price_for("anything") == 0.0

    def test_override_exact(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"web_search": 0.005}})
        assert g.price_for("web_search") == 0.005

    def test_override_glob(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"github.*": 0.002}})
        assert g.price_for("github.create_pr") == 0.002
        assert g.price_for("db_query") == 0.0

    def test_first_match_wins(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"a2a.*": 0.01, "*": 0.001}})
        assert g.price_for("a2a.devops") == 0.01


# ─── Gate decisions (unit) ───────────────────────────────────────────────────

class TestGateDecisions:
    async def test_off_mode_is_free(self):
        g = Gate()
        g.configure({"mode": "off"})
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled and d.free

    async def test_metered_free_action(self):
        g = Gate()
        g.configure({"mode": "metered", "default": 0.0, "wallets": [{"agent_id": "a", "balance_usdc": 1}]})
        d = await g.charge_before(agent_id="a", action="db_query")
        assert d.settled and d.free

    async def test_metered_charges_priced_action(self):
        g = Gate()
        g.configure({
            "mode": "metered", "overrides": {"web_search": 0.005},
            "wallets": [{"agent_id": "a", "balance_usdc": 1.0}],
        })
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled and not d.free
        assert d.amount_usdc == 0.005
        assert g.get_wallet("a").balance_usdc == pytest.approx(0.995)

    async def test_metered_blocks_unfunded(self):
        g = Gate()
        g.configure({
            "mode": "metered", "overrides": {"web_search": 0.005},
            "wallets": [{"agent_id": "a", "balance_usdc": 0.001}],
        })
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled is False
        assert "insufficient balance" in d.reason

    async def test_no_wallet_is_blocked_not_free(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"web_search": 0.005}})
        d = await g.charge_before(agent_id="ghost", action="web_search")
        assert d.settled is False
        assert "no wallet" in d.reason

    async def test_passthrough_not_charged_before(self):
        g = Gate()
        g.configure({"mode": "passthrough", "wallets": [{"agent_id": "a", "balance_usdc": 1}]})
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled and d.free  # passthrough settles reactively, not here


# ─── parse_402 (unit) ────────────────────────────────────────────────────────

class TestParse402:
    def test_none_when_not_402(self):
        assert parse_402({"result": "ok"}, 200, "x") is None

    def test_quote_from_402_body(self):
        q = parse_402({"amount_usdc": 0.005, "pay_to": "0xabc"}, 402, "premium_search")
        assert q is not None
        assert q.amount_usdc == 0.005
        assert q.pay_to == "0xabc"
        assert q.source == "tool_402"

    def test_amount_alias_and_bad_value(self):
        assert parse_402({"amount": 0.01}, 402, "x").amount_usdc == 0.01
        assert parse_402({"amount_usdc": "nope"}, 402, "x").amount_usdc == 0.0


# ─── Integration: metered mode through the proxy ─────────────────────────────

@pytest.fixture
def metered_client(httpserver):
    httpserver.expect_request("/search", method="POST").respond_with_json({"results": ["a", "b"]})
    config = SidecarConfig(
        sidecar_id="pay-test",
        tools=[ToolDefinition(name="web_search", endpoint=httpserver.url_for("/search"))],
        policy=PolicyConfig(allow=["web_search"]),
        payments={
            "mode": "metered",
            "overrides": {"web_search": 0.005},
            "wallets": [
                {"agent_id": "rich", "balance_usdc": 1.0},
                {"agent_id": "broke", "balance_usdc": 0.001},
            ],
        },
    )
    return TestClient(create_app(initial_config=config))


class TestMeteredIntegration:
    def test_funded_agent_pays_and_proceeds(self, metered_client):
        resp = metered_client.post(
            "/tool/web_search", json={"query": "hi"}, headers={"X-Agent-Id": "rich"}
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {"results": ["a", "b"]}
        # balance dropped
        status = metered_client.get("/config/payments").json()
        rich = next(w for w in status["wallets"] if w["agent_id"] == "rich")
        assert rich["balance_usdc"] == pytest.approx(0.995)

    def test_unfunded_agent_blocked_402(self, metered_client):
        resp = metered_client.post(
            "/tool/web_search", json={"query": "hi"}, headers={"X-Agent-Id": "broke"}
        )
        assert resp.status_code == 402
        body = resp.json()
        assert body["blocked"] is True
        assert body["limit_type"] == "payment"
        assert "insufficient balance" in body["reason"]


# ─── Integration: passthrough mode (tool returns 402) ────────────────────────

@pytest.fixture
def passthrough_client(httpserver):
    # Tool 402s without X-PAYMENT, 200s with it.
    def handler(request):
        from werkzeug.wrappers import Response
        if not request.headers.get("X-PAYMENT"):
            return Response(
                '{"amount_usdc": 0.005, "pay_to": "0xmerchant", "nonce": "n1"}',
                status=402, content_type="application/json",
            )
        return Response('{"results": ["premium"]}', status=200, content_type="application/json")

    httpserver.expect_request("/premium", method="POST").respond_with_handler(handler)
    config = SidecarConfig(
        sidecar_id="pay-test",
        tools=[ToolDefinition(name="premium_search", endpoint=httpserver.url_for("/premium"))],
        policy=PolicyConfig(allow=["premium_search"]),
        payments={
            "mode": "passthrough",
            "wallets": [
                {"agent_id": "rich", "balance_usdc": 1.0},
                {"agent_id": "broke", "balance_usdc": 0.001},
            ],
        },
    )
    return TestClient(create_app(initial_config=config))


class TestPassthroughIntegration:
    def test_funded_agent_pays_402_and_gets_result(self, passthrough_client):
        resp = passthrough_client.post(
            "/tool/premium_search", json={"query": "x"}, headers={"X-Agent-Id": "rich"}
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {"results": ["premium"]}
        status = passthrough_client.get("/config/payments").json()
        rich = next(w for w in status["wallets"] if w["agent_id"] == "rich")
        assert rich["balance_usdc"] == pytest.approx(0.995)

    def test_unfunded_agent_blocked_at_402(self, passthrough_client):
        resp = passthrough_client.post(
            "/tool/premium_search", json={"query": "x"}, headers={"X-Agent-Id": "broke"}
        )
        assert resp.status_code == 402
        assert resp.json()["limit_type"] == "payment"


# ─── Integration: off mode charges nothing ───────────────────────────────────

def test_off_mode_never_charges(httpserver):
    httpserver.expect_request("/search", method="POST").respond_with_json({"ok": True})
    config = SidecarConfig(
        sidecar_id="pay-test",
        tools=[ToolDefinition(name="web_search", endpoint=httpserver.url_for("/search"))],
        policy=PolicyConfig(allow=["web_search"]),
        payments={"mode": "off", "wallets": [{"agent_id": "a", "balance_usdc": 0.0}]},
    )
    client = TestClient(create_app(initial_config=config))
    resp = client.post("/tool/web_search", json={"query": "hi"}, headers={"X-Agent-Id": "a"})
    assert resp.status_code == 200  # zero balance but off mode → free
