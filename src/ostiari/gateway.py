"""ActionGateway — risk score aggregation, tier classification, and intervention."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Protocol, runtime_checkable

from ostiari.exceptions import ActionInterventionTimeout
from ostiari.models import (
    AnomalySignal,
    EvalContext,
    GatewayDecision,
    PolicyResult,
    RiskSignal,
    ThresholdConfig,
)

log = logging.getLogger("ostiari")

_callback_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ostiari-intervention")


@runtime_checkable
class SignalProvider(Protocol):
    @property
    def name(self) -> str: ...

    def evaluate(
        self, action: str, params: dict[str, Any], context: EvalContext
    ) -> RiskSignal | None: ...


class ActionGateway:
    def __init__(
        self,
        thresholds: ThresholdConfig | None = None,
        fail_open: bool = True,
        intervention_timeout: float = 30.0,
    ) -> None:
        self._thresholds = thresholds or ThresholdConfig()
        self._fail_open = fail_open
        self._intervention_timeout = intervention_timeout
        self._intervention_callback: Any = None
        self._signal_providers: list[SignalProvider] = []

    def evaluate(
        self,
        action: str,
        params: dict[str, Any],
        context: EvalContext,
        policy_result: PolicyResult | None,
        anomaly_signals: list[AnomalySignal],
    ) -> GatewayDecision:
        thresholds = self._resolve_thresholds(action, policy_result)
        signals: list[RiskSignal] = []

        if policy_result:
            for adj in policy_result.risk_adjustments:
                signals.append(
                    RiskSignal(
                        source="policy",
                        score_contribution=adj.delta,
                        description=adj.reason,
                    )
                )

        for anomaly in anomaly_signals:
            signals.append(
                RiskSignal(
                    source=f"anomaly:{anomaly.detector}",
                    score_contribution=anomaly.score_contribution,
                    description=anomaly.description,
                )
            )

        for provider in self._signal_providers:
            try:
                signal = provider.evaluate(action, params, context)
                if signal is not None:
                    signals.append(signal)
            except Exception as e:
                log.warning("[Ostiari] SignalProvider '%s' failed: %s", provider.name, e)

        score = max(0, min(100, sum(s.score_contribution for s in signals)))
        tier = self._classify(score, thresholds)

        return GatewayDecision(
            tier=tier,
            score=score,
            signals=signals,
            threshold_applied=thresholds,
        )

    def handle_intervention_sync(self, action: str, params: dict[str, Any], score: int) -> str:
        if self._intervention_callback is None:
            if self._fail_open:
                log.warning(
                    "[Ostiari] %s INTERVENE (score=%d) — no callback, allowing (fail_open)",
                    action,
                    score,
                )
                return "allow"
            return "block"

        log.info("[Ostiari] %s INTERVENE (score=%d) — awaiting callback", action, score)
        approved = self._invoke_callback_sync(action, params, score)
        log.info("[Ostiari] %s → callback %s", action, "approved" if approved else "denied")
        return "allow" if approved else "block"

    async def handle_intervention_async(
        self, action: str, params: dict[str, Any], score: int
    ) -> str:
        if self._intervention_callback is None:
            if self._fail_open:
                log.warning(
                    "[Ostiari] %s INTERVENE (score=%d) — no callback, allowing (fail_open)",
                    action,
                    score,
                )
                return "allow"
            return "block"

        log.info("[Ostiari] %s INTERVENE (score=%d) — awaiting callback", action, score)
        approved = await self._invoke_callback_async(action, params, score)
        log.info("[Ostiari] %s → callback %s", action, "approved" if approved else "denied")
        return "allow" if approved else "block"

    def add_signal_provider(self, provider: SignalProvider) -> None:
        if not isinstance(provider, SignalProvider):
            raise TypeError(
                f"Provider must implement SignalProvider protocol, got {type(provider).__name__}"
            )
        self._signal_providers = [*self._signal_providers, provider]

    def set_thresholds(self, allow_max: int, intervene_max: int) -> None:
        self._thresholds = ThresholdConfig(allow_max=allow_max, intervene_max=intervene_max)

    def set_intervention_callback(self, callback: Any) -> None:
        self._intervention_callback = callback

    def _classify(self, score: int, thresholds: ThresholdConfig) -> str:
        if score <= thresholds.allow_max:
            return "allow"
        if score <= thresholds.intervene_max:
            return "intervene"
        return "block"

    def _invoke_callback_sync(self, action: str, params: dict[str, Any], score: int) -> bool:
        callback = self._intervention_callback
        timeout = self._intervention_timeout

        try:
            if asyncio.iscoroutinefunction(callback):

                def _run_async() -> bool:
                    result: bool = asyncio.run(callback(action, params, score))
                    return result

                future = _callback_pool.submit(_run_async)
                return future.result(timeout=timeout)
            else:
                future = _callback_pool.submit(callback, action, params, score)
                return future.result(timeout=timeout)
        except FuturesTimeout:
            raise ActionInterventionTimeout(action, score, timeout) from None
        except ActionInterventionTimeout:
            raise
        except Exception as e:
            log.warning("[Ostiari] Intervention callback failed: %s", e)
            return self._fail_open

    async def _invoke_callback_async(self, action: str, params: dict[str, Any], score: int) -> bool:
        callback = self._intervention_callback
        timeout = self._intervention_timeout

        try:
            if asyncio.iscoroutinefunction(callback):
                return await asyncio.wait_for(callback(action, params, score), timeout)
            else:
                loop = asyncio.get_running_loop()
                return await asyncio.wait_for(
                    loop.run_in_executor(_callback_pool, callback, action, params, score),
                    timeout,
                )
        except asyncio.TimeoutError:
            raise ActionInterventionTimeout(action, score, timeout) from None
        except ActionInterventionTimeout:
            raise
        except Exception as e:
            log.warning("[Ostiari] Intervention callback failed: %s", e)
            return self._fail_open

    def _resolve_thresholds(
        self, action: str, policy_result: PolicyResult | None
    ) -> ThresholdConfig:
        if policy_result and policy_result.effective_thresholds:
            return policy_result.effective_thresholds
        return self._thresholds
