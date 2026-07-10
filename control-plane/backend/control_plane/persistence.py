"""Simple JSON persistence for in-memory stores (quotas, experiments, models).

Saves to data/state.json on shutdown, loads on startup.
Production would use proper DB tables for these.
"""

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("control_plane.persistence")

STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"


def save_state(state: dict[str, Any]) -> None:
    """Save in-memory state to JSON file."""
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    log.info("State saved to %s", STATE_FILE)


def load_state() -> dict[str, Any]:
    """Load state from JSON file (returns empty dict if not found)."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
        log.info("State loaded from %s", STATE_FILE)
        return data
    except Exception as e:
        log.warning("Failed to load state: %s", e)
        return {}
