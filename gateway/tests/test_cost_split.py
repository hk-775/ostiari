"""Tests for accurate input/output token split (budget-accuracy fix)."""

from __future__ import annotations

from ostiari_gateway.modules.llm_gateway.providers import LLMResponse


class TestLLMResponseSplit:
    def test_real_split_preserved(self):
        r = LLMResponse(content="x", model="m", input_tokens=900, output_tokens=100)
        assert r.input_tokens == 900
        assert r.output_tokens == 100
        assert r.tokens_used == 1000

    def test_output_heavy_not_halved(self):
        # the case the 50/50 estimate got wrong: cheap input, expensive output
        r = LLMResponse(content="x", model="m", input_tokens=100, output_tokens=900)
        assert r.output_tokens == 900   # not 500

    def test_estimate_fallback_from_sum(self):
        # legacy path: only a summed tokens_used known -> half/half
        r = LLMResponse(content="x", model="m", tokens_used=1000)
        assert r.input_tokens + r.output_tokens == 1000
        assert r.tokens_used == 1000

    def test_cost_reflects_split(self):
        # a real split priced with output > input costs more than a 50/50 estimate
        from ostiari_gateway.quota_enforcer import QuotaEnforcer
        q = QuotaEnforcer()  # uses DEFAULT_PRICING (claude-sonnet-4-6: in 0.003, out 0.015)
        real = q.calculate_cost("claude-sonnet-4-6", 100, 900)      # output-heavy
        estimate = q.calculate_cost("claude-sonnet-4-6", 500, 500)  # the old 50/50
        assert real > estimate                       # 50/50 under-billed this call
