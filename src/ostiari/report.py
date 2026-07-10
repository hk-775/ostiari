"""Ostiari compliance report generator."""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Any

from ostiari.models import TraceEntry, TraceFilters
from ostiari.storage.protocol import StorageBackend


class ComplianceReport:
    """Structured compliance report data."""

    def __init__(
        self,
        period_start: datetime,
        period_end: datetime,
        stats: dict[str, Any],
        evidence: dict[str, list[dict[str, Any]]],
        status: str = "ok",
    ) -> None:
        self.period_start = period_start
        self.period_end = period_end
        self.stats = stats
        self.evidence = evidence
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "stats": self.stats,
            "evidence": {
                rule_id: [
                    {"trace_id": e["trace_id"], "action": e["action"], "timestamp": e["timestamp"]}
                    for e in entries
                ]
                for rule_id, entries in self.evidence.items()
            },
        }


class ReportGenerator:
    """Generates compliance reports from trace data."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    def generate(self, period_days: int = 7, format: str = "json") -> bytes:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=period_days)

        traces = self._storage.get_traces(TraceFilters(start_time=start, end_time=end, limit=1000))

        if not traces:
            report = ComplianceReport(
                period_start=start,
                period_end=end,
                stats=self._empty_stats(),
                evidence={},
                status="no_activity",
            )
        else:
            stats = self._compute_stats(traces, start, end)
            evidence = self._compile_evidence(traces)
            report = ComplianceReport(
                period_start=start,
                period_end=end,
                stats=stats,
                evidence=evidence,
            )

        if format == "csv":
            return self._to_csv(traces, report)
        return self._to_json(report)

    def generate_csv_rows(self, period_days: int) -> Generator[str, None, None]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=period_days)

        yield "trace_id,timestamp,action,risk_score,tier,duration_ms\n"

        offset = 0
        batch_size = 500
        while True:
            batch = self._storage.get_traces(
                TraceFilters(start_time=start, end_time=end, limit=batch_size, offset=offset)
            )
            if not batch:
                break
            for t in batch:
                yield f"{t.trace_id},{t.timestamp.isoformat()},{t.action},{t.risk_score},{t.tier},{t.duration_ms}\n"
            offset += batch_size

    def _compute_stats(
        self, traces: list[TraceEntry], start: datetime, end: datetime
    ) -> dict[str, Any]:
        total = len(traces)
        allowed = sum(1 for t in traces if t.tier == "allow")
        blocked = sum(1 for t in traces if t.tier == "block")
        intervened = sum(1 for t in traces if t.tier == "intervene")
        avg_risk = sum(t.risk_score for t in traces) / max(total, 1)
        unique_agents = len({t.correlation_id for t in traces if t.correlation_id})

        tool_risks: dict[str, list[int]] = defaultdict(list)
        for t in traces:
            tool_risks[t.action].append(t.risk_score)
        top_risky = sorted(
            [(action, sum(scores) / len(scores)) for action, scores in tool_risks.items()],
            key=lambda x: x[1],
            reverse=True,
        )[:5]

        breaker_trips = sum(1 for t in traces if t.breaker_state == "open")

        return {
            "total_actions": total,
            "allowed": allowed,
            "blocked": blocked,
            "intervened": intervened,
            "avg_risk_score": round(avg_risk, 2),
            "unique_agents": unique_agents,
            "top_risky_tools": [{"action": a, "avg_risk": round(r, 2)} for a, r in top_risky],
            "breaker_trips": breaker_trips,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }

    def _compile_evidence(self, traces: list[TraceEntry]) -> dict[str, list[dict[str, Any]]]:
        blocked = [t for t in traces if t.tier == "block"]
        by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in blocked:
            rule_id = t.metadata.get("rule_triggered", "unknown")
            by_rule[rule_id].append(
                {
                    "trace_id": t.trace_id,
                    "action": t.action,
                    "timestamp": t.timestamp.isoformat(),
                }
            )
        return dict(by_rule)

    def _empty_stats(self) -> dict[str, Any]:
        return {
            "total_actions": 0,
            "allowed": 0,
            "blocked": 0,
            "intervened": 0,
            "avg_risk_score": 0.0,
            "unique_agents": 0,
            "top_risky_tools": [],
            "breaker_trips": 0,
        }

    def _to_json(self, report: ComplianceReport) -> bytes:
        return json.dumps(report.to_dict(), indent=2).encode("utf-8")

    def _to_csv(self, traces: list[TraceEntry], report: ComplianceReport) -> bytes:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["trace_id", "timestamp", "action", "risk_score", "tier", "duration_ms"])
        for t in traces:
            writer.writerow(
                [t.trace_id, t.timestamp.isoformat(), t.action, t.risk_score, t.tier, t.duration_ms]
            )
        return output.getvalue().encode("utf-8")
