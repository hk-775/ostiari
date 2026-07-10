"""Ostiari TUI application — real-time terminal monitoring."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from ostiari.models import TraceFilters
from ostiari.storage import SQLiteBackend
from ostiari.tui.widgets.breakers import BreakerPanel
from ostiari.tui.widgets.metrics import MetricsBar
from ostiari.tui.widgets.stream import ActionStream


class OstiariApp(App):
    """Main Ostiari terminal UI application."""

    TITLE = "Ostiari Monitor"
    CSS = """
    #main { height: 1fr; }
    #sidebar { width: 35; }
    #stream-container { width: 1fr; }
    ActionStream { height: 1fr; }
    BreakerPanel { height: auto; max-height: 50%; }
    MetricsBar { height: 3; dock: bottom; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, db_path: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._db_path = db_path
        self._storage: SQLiteBackend | None = None
        self._last_trace_time: datetime | None = None
        self._connected = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="stream-container"):
                yield ActionStream()
            with Vertical(id="sidebar"):
                yield BreakerPanel()
        yield MetricsBar()
        yield Footer()

    def on_mount(self) -> None:
        try:
            self._storage = SQLiteBackend(path=self._db_path) if self._db_path else SQLiteBackend()
            self._connected = True
        except Exception:
            self._connected = False

        self._last_trace_time = datetime.now(timezone.utc)
        self.set_interval(1.0, self._poll_traces)
        self.set_interval(5.0, self._poll_breakers)

    def _poll_traces(self) -> None:
        if not self._storage:
            return

        try:
            filters = TraceFilters(start_time=self._last_trace_time, limit=50)
            new_traces = self._storage.get_traces(filters)
            if new_traces:
                self._last_trace_time = new_traces[-1].timestamp
                stream = self.query_one(ActionStream)
                for trace in new_traces:
                    stream.add_trace(trace)
                metrics_bar = self.query_one(MetricsBar)
                metrics_bar.update_from_traces(new_traces)
            self._set_connected(True)
        except Exception:
            self._set_connected(False)

    def _poll_breakers(self) -> None:
        if not self._storage:
            return

        try:
            panel = self.query_one(BreakerPanel)
            panel.refresh_states(self._storage)
        except Exception:
            pass

    def _set_connected(self, connected: bool) -> None:
        if connected != self._connected:
            self._connected = connected
            self.sub_title = "" if connected else "[disconnected]"

    def action_refresh(self) -> None:
        self._poll_traces()
        self._poll_breakers()

    def on_unmount(self) -> None:
        if self._storage:
            self._storage.close()
