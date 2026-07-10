"""Intervention modal widget — accept/deny popup for human decisions."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class InterventionModal(ModalScreen[bool]):
    """Modal dialog for human intervention decisions."""

    DEFAULT_CSS = """
    InterventionModal {
        align: center middle;
    }
    #intervention-dialog {
        width: 60;
        height: auto;
        padding: 2;
        border: thick $accent;
        background: $surface;
    }
    #intervention-buttons {
        margin-top: 1;
        align: center middle;
    }
    """

    def __init__(self, action: str, risk_score: float, question: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._action = action
        self._risk_score = risk_score
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="intervention-dialog"):
            yield Label("[bold]Intervention Required[/bold]")
            yield Label(f"Action: {self._action}")
            yield Label(f"Risk Score: {self._risk_score}")
            yield Label(f"\n{self._question}")
            with Vertical(id="intervention-buttons"):
                yield Button("Allow (y)", id="allow-btn", variant="success")
                yield Button("Deny (n)", id="deny-btn", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow-btn")

    def key_y(self) -> None:
        self.dismiss(True)

    def key_n(self) -> None:
        self.dismiss(False)
