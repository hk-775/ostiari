"""Per-agent anomaly history scoping.

The gateway is shared by many agents. Anomaly detection (loops, drift) must see
THIS agent's recent behavior, not a stream diluted by every other agent — else
a loop by one agent hides among the others. These tests verify the eval-context
history is scoped by agent_id.
"""

from __future__ import annotations

import pytest

from ostiari import Guard
from ostiari.exceptions import ActionBlockedError


@pytest.fixture
def guard():
    # Anomaly signals feed the score; loop detection is on by default.
    g = Guard()
    g.start()
    yield g


def _validate(g: Guard, action: str, agent: str, **params):
    return g.validate(action=action, params=params or {"x": 1}, context={"agent_id": agent})


class TestPerAgentAnomalyScoping:
    def test_agent_loop_detected_despite_other_agents(self, guard):
        """agent-A loops one action while agent-B interleaves varied calls.
        A's loop still registers — its anomaly signal escalates A's score —
        because history is scoped to A, not diluted by B. The repeated loop
        drives the score up until it blocks, which is the correct outcome.
        """
        loop_detected = False
        try:
            for _ in range(12):
                _validate(guard, "varied", "agent-B")   # noisy neighbor, varied
                r = guard.validate(
                    action="poll_status", params={"x": 1}, context={"agent_id": "agent-A"}
                )
                if any("loop" in s.source.lower() for s in r.signals):
                    loop_detected = True
                    break
        except ActionBlockedError:
            # Loop drove A's score past the block threshold — the feature working.
            loop_detected = True
        assert loop_detected, "agent-A's loop should register despite agent-B's interleaved calls"

    def test_varied_agent_not_flagged(self, guard):
        """agent-B, doing all-different actions, must NOT trip the loop detector."""
        for i in range(8):
            _validate(guard, "poll_status", "agent-A")   # noisy neighbor loops
            _validate(guard, f"unique_{i}", "agent-B")

        b_result = guard.validate(
            action="another_unique", params={"x": 1}, context={"agent_id": "agent-B"}
        )
        b_loop = any("loop" in s.source.lower() for s in b_result.signals)
        assert not b_loop, "agent-B does varied work; its scoped history is not a loop"

    def test_trace_records_agent_id(self, guard):
        _validate(guard, "some_action", "agent-Z")
        # The recorded trace carries the agent id (that's what enables scoping).
        hist = guard._tracer.recent_history(5)
        assert any(getattr(h, "agent_id", "") == "agent-Z" for h in hist)
