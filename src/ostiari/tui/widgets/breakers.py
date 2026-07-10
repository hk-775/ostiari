"""Breaker panel widget — circuit breaker state display."""

from __future__ import annotations

from typing import Any

from textual.widgets import Static

STATE_INDICATORS = {
    "closed": "[green]●[/green]",
    "open": "[red]●[/red]",
    "half_open": "[yellow]●[/yellow]",
}


class BreakerPanel(Static):
    """Displays circuit breaker states with counters and thresholds."""

    DEFAULT_CSS = """
    BreakerPanel {
        padding: 1;
        border: solid $accent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("Circuit Breakers\n─────────────────\nNo breakers configured", **kwargs)

    def refresh_states(self, storage: Any) -> None:
        from ostiari.models import MetricType

        lines = ["Circuit Breakers", "─────────────────"]
        breaker_ids = [m.value for m in MetricType]
        found_any = False

        for bid in breaker_ids:
            state = storage.get_breaker_state(bid)
            if state is None:
                continue
            found_any = True
            indicator = STATE_INDICATORS.get(state.state, "?")
            counter = state.metrics.get("counter", 0)
            lines.append(f"{indicator} {bid}: {state.state} (counter={counter:.1f})")

        if not found_any:
            lines.append("No breakers configured")

        self.update("\n".join(lines))
