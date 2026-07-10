"""Metrics bar widget — summary statistics display."""

from __future__ import annotations

from textual.widgets import Static

from ostiari.models import TraceEntry


class MetricsBar(Static):
    """Summary metrics bar showing total, allowed, blocked, avg risk."""

    DEFAULT_CSS = """
    MetricsBar {
        background: $surface;
        padding: 0 2;
        content-align: center middle;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("Total: 0 | Allowed: 0 | Blocked: 0 | Avg Risk: 0", **kwargs)
        self._total = 0
        self._allowed = 0
        self._blocked = 0
        self._risk_sum = 0

    def update_from_traces(self, traces: list[TraceEntry]) -> None:
        for t in traces:
            self._total += 1
            self._risk_sum += t.risk_score
            if t.tier == "allow":
                self._allowed += 1
            elif t.tier == "block":
                self._blocked += 1

        avg = self._risk_sum / max(self._total, 1)
        self.update(
            f"Total: {self._total} | "
            f"[green]Allowed: {self._allowed}[/green] | "
            f"[red]Blocked: {self._blocked}[/red] | "
            f"Avg Risk: {avg:.1f}"
        )
