"""Action stream widget — scrollable trace list with color coding."""

from __future__ import annotations

from collections import deque

from textual.widgets import Static

from ostiari.models import TraceEntry

TIER_COLORS = {"allow": "green", "intervene": "yellow", "block": "red"}
MAX_VISIBLE = 100


class ActionStream(Static):
    """Scrollable list of recent actions with color-coded tiers."""

    DEFAULT_CSS = """
    ActionStream {
        overflow-y: auto;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: deque[TraceEntry] = deque(maxlen=MAX_VISIBLE)

    def add_trace(self, trace: TraceEntry) -> None:
        self._entries.append(trace)
        self._render_entries()

    def _render_entries(self) -> None:
        lines: list[str] = []
        for t in self._entries:
            color = TIER_COLORS.get(t.tier, "white")
            time_str = t.timestamp.strftime("%H:%M:%S")
            tier_label = f"[{color}][{t.tier.upper():10s}][/{color}]"
            line = f"{time_str} {tier_label} {t.action:30s} score={t.risk_score:3d} {t.duration_ms:.0f}ms"
            lines.append(line)
        self.update("\n".join(lines))
