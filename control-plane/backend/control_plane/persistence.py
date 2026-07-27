"""Simple JSON persistence for in-memory stores (quotas, experiments, models).

Saves to state.json in the data dir on shutdown, loads on startup.
Production would use proper DB tables for these.
"""

import json
import logging
from typing import Any

from control_plane.env import data_dir

log = logging.getLogger("control_plane.persistence")

# Same directory as the SQLite database (control_plane.env.data_dir). These two
# used to disagree — db in control-plane/data, this one in
# control-plane/backend/data — because each derived its own path from __file__ with
# a different number of hops. That split was invisible in a dev checkout but broke
# the container: DATABASE_URL redirected only the db onto the mounted volume, so
# this resolved to /app/data, root-owned and uncreatable by the non-root user, and
# save_state raised PermissionError on every shutdown.
STATE_FILE = data_dir() / "state.json"


def save_state(state: dict[str, Any]) -> None:
    """Save in-memory state to JSON file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
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
