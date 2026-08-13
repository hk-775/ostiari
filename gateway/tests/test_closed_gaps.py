"""Gateway-side tests for the previously-documented config gaps.

Covers: enforcement mode surviving a config bundle, budget alerts actually
firing (including under a shared store), operator pricing beating the built-in
table, and A/B experiments arriving as a partial update.
"""

from ostiari_gateway.quota_enforcer import DEFAULT_PRICING, QuotaEnforcer


def _app(mode="enforce", control_plane_url="http://cp.invalid"):
    """A gateway app with the control-plane bundle applier wired up."""
    from ostiari_gateway.models import SidecarConfig
    from ostiari_gateway.server import create_app

    return create_app(SidecarConfig(
        sidecar_id="gw-test", mode=mode, control_plane_url=control_plane_url))


class TestEnforcementMode:
    """`mode` in a bundle must be applied — _apply_bundle used to ignore it, so a
    gateway the operator left in shadow reverted to enforce on every restart."""

    def test_bundle_switches_enforce_to_shadow(self):
        app = _app(mode="enforce")
        app.state.apply_bundle({"mode": "shadow", "tools": [], "policy": {}})
        assert app.state.manager.config.mode == "shadow"

    def test_bundle_switches_shadow_to_enforce(self):
        app = _app(mode="shadow")
        app.state.apply_bundle({"mode": "enforce"})
        assert app.state.manager.config.mode == "enforce"

    def test_bundle_without_mode_leaves_it_alone(self):
        app = _app(mode="shadow")
        app.state.apply_bundle({"tools": [], "policy": {}})
        assert app.state.manager.config.mode == "shadow"

    def test_unknown_mode_is_ignored_not_defaulted(self):
        """Never guess: an unrecognized mode must not fall back to enforce on a
        gateway that was deliberately observing."""
        app = _app(mode="shadow")
        app.state.apply_bundle({"mode": "enfroce"})
        assert app.state.manager.config.mode == "shadow"

    def test_config_endpoint_applies_mode(self):
        from ostiari_gateway.models import SidecarConfig
        from ostiari_gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app(SidecarConfig(sidecar_id="gw-test", mode="enforce"))
        with TestClient(app) as c:
            r = c.post("/config", json={"sidecar_id": "gw-test", "mode": "shadow",
                                        "tools": [], "policy": {}})
            assert r.status_code == 200
            assert c.get("/config").json()["mode"] == "shadow"


class TestBudgetAlerts:
    def test_callback_fires_at_each_threshold(self):
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        fired = []
        q.on_budget_alert(lambda label, spend, budget: fired.append(label))
        q.record_spend(0.85)
        q.record_spend(0.06)
        q.record_spend(0.10)
        assert fired == ["80%", "90%", "100%"]

    def test_each_threshold_fires_once(self):
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        fired = []
        q.on_budget_alert(lambda *a: fired.append(a[0]))
        for _ in range(5):
            q.record_spend(0.17)
        assert fired.count("80%") == 1

    def test_callback_receives_real_spend(self):
        """A single spend can cross more than one threshold; each callback still
        reports the actual spend and the configured limit."""
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 10.0})
        seen = []
        q.on_budget_alert(lambda label, spend, budget: seen.append((label, spend, budget)))
        q.record_spend(9.0)
        assert seen == [("80%", 9.0, 10.0), ("90%", 9.0, 10.0)]

    def test_fires_with_shared_store(self):
        """Reading _total_spend meant no alert ever fired under Redis, because
        spend is booked to the store and _total_spend stays 0.0 — fleet
        deployments are exactly where alerting matters."""
        class _Store:
            def __init__(self): self.spend = 0.0
            def budget_spend(self, key): return self.spend
            def budget_adjust(self, key, delta): self.spend += delta
            def budget_reserve(self, key, amount, limit):
                if self.spend + amount >= limit:
                    return False
                self.spend += amount
                return True
            def budget_reset(self, key): self.spend = 0.0

        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        q.attach_shared_store(_Store())
        fired = []
        q.on_budget_alert(lambda label, spend, budget: fired.append((label, spend)))
        q.record_spend(0.85)
        assert fired and fired[0][0] == "80%" and fired[0][1] == 0.85

    def test_one_broken_callback_does_not_stop_the_others(self):
        q = QuotaEnforcer()
        q.configure({"budget_limit_usd": 1.0})
        ok = []

        def _boom(*a):
            raise RuntimeError("subscriber blew up")

        q.on_budget_alert(_boom)
        q.on_budget_alert(lambda *a: ok.append(a[0]))
        q.record_spend(0.9)          # must not raise
        assert ok == ["80%", "90%"]


class TestPricingPrecedence:
    def test_pushed_price_wins_over_builtin(self):
        q = QuotaEnforcer()
        q.configure({"pricing": {"gpt-4o": {"input": 99.0, "output": 99.0}}})
        assert q._get_pricing("gpt-4o") == {"input": 99.0, "output": 99.0}

    def test_exact_pushed_beats_fuzzy_builtin(self):
        """The fuzzy loop used to scan only DEFAULT_PRICING, so a pushed
        "gpt-4o-mini" could lose to a fuzzy hit on built-in "gpt-4o" — 16x more
        expensive, in the wrong direction for a budget."""
        q = QuotaEnforcer()
        q.configure({"pricing": {"gpt-4o-mini": {"input": 0.0001, "output": 0.0002}}})
        assert q._get_pricing("gpt-4o-mini") == {"input": 0.0001, "output": 0.0002}

    def test_fuzzy_falls_through_to_pushed_first(self):
        """Bedrock-style ids are only ever reachable via pushed pricing."""
        q = QuotaEnforcer()
        q.configure({"pricing": {"claude-sonnet-4-6": {"input": 0.111, "output": 0.222}}})
        got = q._get_pricing("us.anthropic.claude-sonnet-4-6-v1:0")
        assert got == {"input": 0.111, "output": 0.222}

    def test_builtin_used_when_nothing_pushed(self):
        q = QuotaEnforcer()
        q.configure({})
        assert q._get_pricing("gpt-4o") == DEFAULT_PRICING["gpt-4o"]

    def test_unknown_model_gets_midrange_fallback(self):
        q = QuotaEnforcer()
        q.configure({})
        assert q._get_pricing("totally-made-up-xyz") == {"input": 0.003, "output": 0.015}

    def test_pushed_pricing_changes_calculated_cost(self):
        """The point of pushing pricing: the enforced budget uses the operator's
        numbers, not the built-in table's."""
        q = QuotaEnforcer()
        q.configure({"pricing": {"m1": {"input": 1.0, "output": 2.0}}})
        # 1000 in / 1000 out at per-1k prices = 1.0 + 2.0
        assert q.calculate_cost("m1", 1000, 1000) == 3.0


class TestABExperimentsPartialUpdate:
    def _module(self):
        from ostiari_gateway.modules.llm_gateway.models import LLMConfig
        from ostiari_gateway.modules.llm_gateway.module import LLMGatewayModule

        m = LLMGatewayModule()
        m._config = LLMConfig(default_model="claude-sonnet-4-6",
                              fallback_chain=["gpt-4o"])
        return m

    def test_apply_replaces_set(self):
        m = self._module()
        m.apply_ab_experiments([{"name": "e1", "model_a": "a", "model_b": "b",
                                 "traffic_pct_b": 30}])
        assert [e.name for e in m._config.ab_experiments] == ["e1"]
        m.apply_ab_experiments([{"name": "e2", "model_a": "a", "model_b": "b"}])
        assert [e.name for e in m._config.ab_experiments] == ["e2"]

    def test_empty_list_clears(self):
        m = self._module()
        m.apply_ab_experiments([{"name": "e1", "model_a": "a", "model_b": "b"}])
        m.apply_ab_experiments([])
        assert m._config.ab_experiments == []

    def test_other_llm_config_preserved(self):
        """This is why it isn't pushed through /config/llm — that endpoint is a
        whole-document replace and would wipe the startup credentials."""
        m = self._module()
        m._config.credentials.openai = "sk-secret"
        m.apply_ab_experiments([{"name": "e1", "model_a": "a", "model_b": "b"}])
        assert m._config.default_model == "claude-sonnet-4-6"
        assert m._config.fallback_chain == ["gpt-4o"]
        assert m._config.credentials.openai == "sk-secret"

    def test_router_honors_applied_experiment(self):
        """End of the chain: an applied experiment actually splits traffic."""
        from ostiari_gateway.modules.llm_gateway.router import ModelRouter

        m = self._module()
        m.apply_ab_experiments([{"name": "e1", "model_a": "model-a",
                                 "model_b": "model-b", "traffic_pct_b": 100}])
        router = ModelRouter(config=m._config)
        # 100% to B: every agent lands on the treatment model.
        assert router.select_model({"agent_id": "any-agent"}) == "model-b"

    def test_disabled_experiment_ignored(self):
        from ostiari_gateway.modules.llm_gateway.router import ModelRouter

        m = self._module()
        m.apply_ab_experiments([{"name": "e1", "model_a": "model-a", "model_b": "model-b",
                                 "traffic_pct_b": 100, "enabled": False}])
        router = ModelRouter(config=m._config)
        assert router.select_model({"agent_id": "any-agent"}) == "claude-sonnet-4-6"


class TestBundleAppliesExperiments:
    """The bundle path, so an experiment survives a gateway restart."""

    def test_bundle_reaches_the_llm_module(self):
        from ostiari_gateway.models import ModulesConfig, SidecarConfig
        from ostiari_gateway.server import create_app

        app = create_app(SidecarConfig(
            sidecar_id="gw-test", control_plane_url="http://cp.invalid",
            modules=ModulesConfig(llm_gateway=True)))
        app.state.apply_bundle({"ab_experiments": [
            {"name": "e1", "model_a": "a", "model_b": "b", "traffic_pct_b": 40}]})
        mod = app.state.module_registry.get("llm_gateway")
        assert [e.name for e in mod._config.ab_experiments] == ["e1"]
        assert mod._config.ab_experiments[0].traffic_pct_b == 40

    def test_malformed_experiment_does_not_abort_the_bundle(self):
        """Tools and policy are the safety-critical parts of a bundle — a bad
        experiment must not stop them being applied."""
        from ostiari_gateway.models import ModulesConfig, SidecarConfig
        from ostiari_gateway.server import create_app

        app = create_app(SidecarConfig(
            sidecar_id="gw-test", mode="shadow", control_plane_url="http://cp.invalid",
            modules=ModulesConfig(llm_gateway=True)))
        app.state.apply_bundle({
            "mode": "enforce",
            "ab_experiments": [{"name": "broken"}],   # missing model_a/model_b
        })
        assert app.state.manager.config.mode == "enforce"
