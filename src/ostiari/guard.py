"""Guard — central mediator orchestrating the Ostiari evaluation pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ostiari.anomaly import AnomalyDetector
from ostiari.breaker import CircuitBreaker
from ostiari.checkpoint import CheckpointEngine
from ostiari.exceptions import ActionBlockedError, OstiariError
from ostiari.gateway import ActionGateway
from ostiari.models import (
    AnomalySignal,
    BreakerConfig,
    CheckpointID,
    CheckpointState,
    EvalContext,
    GatewayDecision,
    MetricType,
    OstiariConfig,
    RetentionPolicy,
    TraceEntry,
    ValidationResult,
)
from ostiari.policy import PolicyEngine
from ostiari.storage.redaction import RedactionFilter
from ostiari.tracer import ExecutionTracer

log = logging.getLogger("ostiari")


class Guard:
    def __init__(
        self,
        config: OstiariConfig | None = None,
        policy_engine: PolicyEngine | None = None,
        anomaly_detector: AnomalyDetector | None = None,
        storage: Any = None,
        breaker_configs: list[BreakerConfig] | None = None,
        retention_policy: RetentionPolicy | None = None,
        adapter: Any = None,
        policy_source: str | None = None,
        parameter_risk: bool = True,
    ) -> None:
        self._config = config or OstiariConfig()
        self._fail_open = self._config.fail_open

        self._policy_engine = policy_engine or PolicyEngine()
        self._anomaly_detector = anomaly_detector or AnomalyDetector()
        self._gateway = ActionGateway(
            thresholds=self._config.thresholds,
            fail_open=self._fail_open,
            intervention_timeout=30.0,
        )

        # Parameter-aware risk: score by what the call actually does (blast
        # radius, target sensitivity, destructiveness), not just the action
        # name. On by default; pass parameter_risk=False to disable.
        if parameter_risk:
            from ostiari.signals import ParameterRiskSignal
            self._gateway.add_signal_provider(ParameterRiskSignal())

        self._storage = storage
        self._tracer = ExecutionTracer(storage=self._storage)
        self._redaction = RedactionFilter(self._config.redact_patterns)

        self._breaker: CircuitBreaker | None = None
        if breaker_configs:
            self._breaker = CircuitBreaker(
                storage=self._storage,
                tracer=self._tracer,
                configs=breaker_configs,
                persist_queue=self._tracer.persist_queue,
            )

        self._checkpoint_engine: CheckpointEngine | None = CheckpointEngine(
            storage=self._storage,
            retention=retention_policy,
            persist_queue=self._tracer.persist_queue,
        )

        self._adapters: list[Any] = []
        self._policy_source = policy_source
        self._policy_poller: Any = None
        self._state = "created"

        if adapter is not None:
            adapters = adapter if isinstance(adapter, list) else [adapter]
            for a in adapters:
                self.register_adapter(a)

    def validate(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        if self._state == "shutdown":
            raise OstiariError("Guard has been shut down")

        start_time = time.monotonic()
        params = params or {}
        context = context or {}
        errors: list[str] = []

        probing: str | None = None
        if self._breaker is not None:
            probing = self._breaker.check()

        try:
            return self._execute_pipeline(action, params, context, errors, start_time, probing)
        except ActionBlockedError:
            if probing and self._breaker is not None:
                self._breaker.report_probe_result(probing, success=False)
            raise

    def _execute_pipeline(
        self,
        action: str,
        params: dict[str, Any],
        context: dict[str, Any],
        errors: list[str],
        start_time: float,
        probing: str | None,
    ) -> ValidationResult:
        adapter_context = self._run_adapter_pre_hooks(action, params)
        if adapter_context is not None:
            action = adapter_context.action
            params = adapter_context.params

        eval_context = self._build_eval_context(context)

        policy_result = self._safe_call(
            self._policy_engine.evaluate, errors, action, params, eval_context
        )

        if policy_result and policy_result.decision == "block":
            duration_ms = (time.monotonic() - start_time) * 1000
            rule_desc = (
                policy_result.blocked_by.description if policy_result.blocked_by else "policy block"
            )
            rule_id = policy_result.blocked_by.action if policy_result.blocked_by else None
            log.info("[Ostiari] %s BLOCKED by %s: %s", action, rule_id, rule_desc)
            self._record_trace(
                action,
                params,
                score=100,
                tier="block",
                duration_ms=duration_ms,
                errors=errors,
                signals=[],
                anomalies=[],
                rule_triggered=rule_id,
            )
            self._record_breaker_metrics(duration_ms, context, is_block=True)
            raise ActionBlockedError(
                action=action,
                params=self._redaction.redact(params),
                score=100,
                rule_id=rule_id,
                reason=rule_desc or "Blocked by policy",
            )

        anomaly_signals: list[AnomalySignal] = (
            self._safe_call(
                self._anomaly_detector.analyze,
                errors,
                action,
                params,
                eval_context.history,
            )
            or []
        )

        gateway_decision: GatewayDecision | None = self._safe_call(
            self._gateway.evaluate,
            errors,
            action,
            params,
            eval_context,
            policy_result,
            anomaly_signals,
        )

        if gateway_decision is None:
            if self._fail_open:
                gateway_decision = GatewayDecision(
                    tier="allow",
                    score=0,
                    signals=[],
                    threshold_applied=self._config.thresholds,
                )
            else:
                raise OstiariError(f"Pipeline failure during evaluation of '{action}'")

        final_tier: str = gateway_decision.tier

        if final_tier == "intervene":
            redacted_params = self._redaction.redact(params)
            final_tier = self._gateway.handle_intervention_sync(
                action, redacted_params, gateway_decision.score
            )

        duration_ms = (time.monotonic() - start_time) * 1000

        self._record_trace(
            action,
            params,
            score=gateway_decision.score,
            tier=final_tier,
            duration_ms=duration_ms,
            errors=errors,
            signals=gateway_decision.signals,
            anomalies=anomaly_signals,
            rule_triggered=gateway_decision.rule_triggered,
        )

        if final_tier == "block":
            log.info(
                "[Ostiari] %s BLOCKED (score=%d)",
                action,
                gateway_decision.score,
            )
            self._record_breaker_metrics(duration_ms, context, is_block=True)
            raise ActionBlockedError(
                action=action,
                params=self._redaction.redact(params),
                score=gateway_decision.score,
                rule_id=gateway_decision.rule_triggered,
                reason="Risk score exceeded block threshold",
            )

        if probing and self._breaker is not None:
            self._breaker.report_probe_result(probing, success=True)

        self._record_breaker_metrics(duration_ms, context, is_block=False)
        self._auto_checkpoint(action, params, final_tier)

        log.info(
            "[Ostiari] %s → score=%d tier=%s (%.1fms)",
            action,
            gateway_decision.score,
            final_tier,
            duration_ms,
        )

        result = ValidationResult(
            tier=final_tier,
            score=gateway_decision.score,
            signals=gateway_decision.signals,
            trace_id=str(uuid.uuid4()),
            action=action,
            params=self._redaction.redact(params),
            duration_ms=duration_ms,
            rule_triggered=gateway_decision.rule_triggered,
        )

        self._run_adapter_post_hooks(adapter_context, result)
        return result

    async def avalidate(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        return await asyncio.to_thread(self.validate, action, params, context)

    def start(self) -> None:
        if self._state == "started":
            return
        if self._state == "shutdown":
            raise OstiariError("Cannot restart a shut-down Guard")
        self._tracer.start()
        if self._breaker is not None:
            self._breaker.restore_state()
        if self._policy_source is not None:
            from ostiari.policy.fetcher import PolicySource
            from ostiari.policy.poller import PolicyPoller

            source = PolicySource(url=self._policy_source)
            self._policy_poller = PolicyPoller(source=source, engine=self._policy_engine)
            self._policy_poller.start()
        self._state = "started"

    def shutdown(self) -> None:
        if self._state == "shutdown":
            return
        if self._policy_poller is not None:
            self._policy_poller.stop()
            self._policy_poller = None
        self._tracer.shutdown()
        self._state = "shutdown"

    def register_detector(self, detector: Any) -> None:
        self._anomaly_detector.register_custom(detector)

    def register_adapter(self, adapter: Any) -> None:
        from ostiari.adapters.protocol import validate_adapter

        validate_adapter(adapter)
        self._adapters = [*self._adapters, adapter]

    def _run_adapter_pre_hooks(self, action: str, params: dict[str, Any]) -> Any:
        if not self._adapters:
            return None
        from ostiari.adapters.protocol import AdapterContext

        context: AdapterContext | None = None
        for adapter in self._adapters:
            try:
                ctx = adapter.wrap_tool_call(action, params)
                if context is None:
                    context = ctx
            except Exception as e:
                log.warning("Adapter %s.wrap_tool_call failed: %s", adapter.name, e)
        return context

    def _run_adapter_post_hooks(self, context: Any, result: Any) -> None:
        if not self._adapters or context is None:
            return
        for adapter in self._adapters:
            try:
                adapter.on_result(context, result)
            except Exception as e:
                log.warning("Adapter %s.on_result failed: %s", adapter.name, e)

    def _run_adapter_error_hooks(self, context: Any, error: Exception) -> None:
        if not self._adapters or context is None:
            return
        for adapter in self._adapters:
            try:
                adapter.on_error(context, error)
            except Exception as e:
                log.warning("Adapter %s.on_error failed: %s", adapter.name, e)

    def configure(self, config: dict[str, Any] | str | Path) -> None:
        if isinstance(config, (str, Path)):
            self._policy_engine.load([str(config)])
        else:
            self._config = self._config.model_copy(update=config)
            if "thresholds" in config:
                self._gateway.set_thresholds(
                    self._config.thresholds.allow_max,
                    self._config.thresholds.intervene_max,
                )

    def checkpoint(
        self, name: str | None = None, state: dict[str, Any] | None = None
    ) -> CheckpointID:
        if self._checkpoint_engine is None:
            raise OstiariError("Checkpoint engine not available")
        return self._checkpoint_engine.create(
            action="manual_checkpoint", params={}, name=name, state=state
        )

    def rollback(self, to: str) -> CheckpointState:
        if self._checkpoint_engine is None:
            raise OstiariError("Checkpoint engine not available")
        return self._checkpoint_engine.rollback(to=to)

    def report_outcome(self, action: str, success: bool, error: str | None = None) -> None:
        if self._breaker is not None:
            self._breaker.report_outcome(action, success, error)

    def configure_breakers(self, configs: list[BreakerConfig]) -> None:
        if self._breaker is None:
            self._breaker = CircuitBreaker(
                storage=self._storage,
                tracer=self._tracer,
                configs=configs,
                persist_queue=self._tracer.persist_queue,
            )
        else:
            self._breaker.configure(configs)

    @property
    def state(self) -> str:
        return self._state

    @property
    def tracer(self) -> ExecutionTracer:
        return self._tracer

    @property
    def gateway(self) -> ActionGateway:
        return self._gateway

    def __enter__(self) -> Guard:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()

    def _safe_call(self, fn: Any, errors: list[str], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as e:
            fn_name = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
            errors.append(f"{fn_name}: {e}")
            log.warning("[Ostiari] %s failed: %s", fn_name, e)
            if not self._fail_open:
                raise OstiariError(f"Pipeline failure in {fn_name}: {e}") from e
            return None

    def _build_eval_context(self, context: dict[str, Any]) -> EvalContext:
        history = self._tracer.recent_history(20)
        return EvalContext(
            history=history,
            current_time=datetime.now(timezone.utc),
            correlation_id=self._tracer.correlation_id,
            metadata=context,
        )

    def _record_breaker_metrics(
        self, duration_ms: float, context: dict[str, Any], is_block: bool
    ) -> None:
        if self._breaker is None:
            return
        try:
            self._breaker.record(MetricType.WALL_CLOCK_MS, duration_ms)
            self._breaker.record(MetricType.TOTAL_ACTIONS, 1.0)
            if "token_cost" in context:
                self._breaker.record(MetricType.TOKEN_COST, float(context["token_cost"]))
            if is_block:
                self._breaker.record(MetricType.ERROR_COUNT, 1.0)
        except Exception as e:
            log.warning("[Ostiari] Breaker metric recording failed: %s", e)

    def _auto_checkpoint(self, action: str, params: dict[str, Any], tier: str) -> None:
        if self._checkpoint_engine is None:
            return
        if not self._checkpoint_engine.auto_enabled:
            return
        if tier == "block":
            return
        try:
            self._checkpoint_engine.create(
                action=action,
                params=self._redaction.redact(params),
            )
        except Exception as e:
            log.warning("[Ostiari] Auto-checkpoint failed: %s", e)

    def _record_trace(
        self,
        action: str,
        params: dict[str, Any],
        score: int,
        tier: str,
        duration_ms: float,
        errors: list[str],
        signals: list[Any],
        anomalies: list[Any],
        rule_triggered: str | None = None,
    ) -> None:
        try:
            metadata: dict[str, Any] = {}
            if errors:
                metadata["errors"] = errors
            if rule_triggered:
                metadata["rule_triggered"] = rule_triggered
            version = self._policy_engine.current_version
            metadata["policy_version"] = version.hash
            metadata["policy_source"] = version.source

            entry = TraceEntry(
                trace_id=str(uuid.uuid4()),
                correlation_id=self._tracer.correlation_id,
                timestamp=datetime.now(timezone.utc),
                action=action,
                params=self._redaction.redact(params),
                risk_score=score,
                tier=tier,
                duration_ms=duration_ms,
                signals=signals,
                anomalies=anomalies,
                metadata=metadata,
            )
            self._tracer.record(entry)
        except Exception as e:
            log.warning("[Ostiari] Trace recording failed: %s", e)
