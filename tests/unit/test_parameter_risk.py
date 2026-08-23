"""Unit tests for the parameter-aware risk signal."""

from __future__ import annotations

import pytest

from ostiari import Guard
from ostiari.exceptions import ActionBlockedError
from ostiari.gateway import ActionGateway
from ostiari.models import EvalContext, OstiariConfig, PolicyResult, ThresholdConfig
from ostiari.signals.parameter_risk import ParameterRiskSignal
from ostiari.testing import MockStorage


def _ctx() -> EvalContext:
    return EvalContext()


def _sig(action: str, params: dict):
    return ParameterRiskSignal().evaluate(action, params, _ctx())


class TestParameterRiskSignal:
    def test_no_params_no_signal(self):
        assert _sig("db_delete", {}) is None

    def test_benign_params_no_signal(self):
        # scoped delete of one row → no risk parameters
        assert _sig("db_delete", {"sql": "DELETE FROM orders WHERE id = 5"}) is None

    def test_unbounded_delete_scores(self):
        s = _sig("db_delete", {"sql": "DELETE FROM users WHERE 1=1"})
        assert s is not None and s.score_contribution >= 40
        assert "unbounded" in s.description.lower()

    def test_delete_without_where_scores(self):
        s = _sig("db_delete", {"sql": "DELETE FROM users"})
        assert s is not None and s.score_contribution >= 40

    def test_truncate_and_drop_score(self):
        assert _sig("db", {"sql": "TRUNCATE TABLE t"}).score_contribution >= 40
        assert _sig("db", {"sql": "DROP TABLE t"}).score_contribution >= 40

    def test_mass_keyword_scores(self):
        s = _sig("file_delete", {"target": "delete all files"})
        assert s is not None and s.score_contribution > 0

    def test_production_target_scores(self):
        s = _sig("deploy", {"environment": "production", "service": "auth"})
        assert s is not None and "production" in s.description.lower()

    def test_privileged_target_scores(self):
        s = _sig("read", {"path": "/etc/secret/private_key"})
        assert s is not None and s.score_contribution >= 30

    def test_process_boundary_escape_scores_at_block_tier(self):
        s = _sig(
            "process.exec",
            {
                "resource": "/workspace/package-probe",
                "boundary": "range",
                "escape_sequence": "synthetic-boundary-confusion",
            },
        )
        assert s is not None and s.score_contribution >= 80
        assert "outside the assigned boundary" in s.description

    def test_explicit_host_boundary_scores_at_block_tier(self):
        s = _sig("command.run", {"boundary": "host", "command": "synthetic"})
        assert s is not None and s.score_contribution >= 80

    def test_bounded_process_call_has_no_boundary_signal(self):
        assert _sig(
            "process.exec",
            {"boundary": "range", "command": "synthetic-helper"},
        ) is None

    def test_control_plane_service_target_requires_intervention(self):
        s = _sig(
            "service.call",
            {
                "destination": "artifact-proxy.mock",
                "target": "package-control.mock",
            },
        )
        assert s is not None and s.score_contribution >= 45
        assert "control-plane" in s.description

    def test_regular_service_target_has_no_control_plane_signal(self):
        assert _sig(
            "service.call",
            {
                "destination": "service-proxy.mock",
                "target": "catalog.mock",
            },
        ) is None

    def test_high_count_scores(self):
        s = _sig("send_email", {"count": 50000, "subject": "blast"})
        assert s is not None and "high volume" in s.description.lower()

    def test_external_recipient_scores(self):
        s = _sig("send_email", {"to": "attacker@evil.com", "subject": "data"})
        assert s is not None and "external" in s.description.lower()

    def test_internal_recipient_no_recipient_risk(self):
        # internal domain → not flagged as external
        s = _sig("send_email", {"to": "teammate@example.com", "subject": "hi"})
        # may be None (no other risk) or, if flagged, not for external reasons
        if s is not None:
            assert "external" not in s.description.lower()

    def test_score_is_capped(self):
        # pile on every heuristic → still capped
        s = _sig("db_delete", {
            "sql": "DELETE FROM prod WHERE 1=1; DROP TABLE secret",
            "environment": "production", "count": 999999,
            "to": "x@evil.com", "target": "delete everything *",
        })
        assert s is not None and s.score_contribution <= 80

    def test_metadata_lists_reasons(self):
        s = _sig("db_delete", {"sql": "DELETE FROM users WHERE 1=1"})
        assert s.metadata.get("reasons")


class TestThroughGateway:
    """The whole point: scoped vs. mass operation must score differently."""

    def _gw(self):
        gw = ActionGateway()
        gw.add_signal_provider(ParameterRiskSignal())
        return gw

    def test_scoped_vs_mass_delete_differ(self):
        gw = self._gw()
        pr = PolicyResult(decision="evaluate", risk_adjustments=[])
        scoped = gw.evaluate("db_delete", {"sql": "DELETE FROM orders WHERE id=5"}, EvalContext(), pr, [])
        mass = gw.evaluate("db_delete", {"sql": "DELETE FROM orders WHERE 1=1"}, EvalContext(), pr, [])
        assert mass.score > scoped.score
        # same action name — the difference comes purely from parameters
        assert scoped.score == 0
        assert mass.score >= 40

    def test_signal_appears_in_decision(self):
        gw = self._gw()
        pr = PolicyResult(decision="evaluate", risk_adjustments=[])
        d = gw.evaluate("send_email", {"to": "x@evil.com"}, EvalContext(), pr, [])
        assert any(sig.source == "parameter-risk" for sig in d.signals)


class TestFailClosedIncidentRegression:
    def _guard(self) -> Guard:
        guard = Guard(
            config=OstiariConfig(
                fail_open=False,
                thresholds=ThresholdConfig(allow_max=30, intervene_max=70),
            ),
            storage=MockStorage(),
            parameter_risk=True,
        )
        guard.start()
        return guard

    def test_boundary_crossing_is_blocked(self):
        guard = self._guard()
        try:
            with pytest.raises(ActionBlockedError) as raised:
                guard.validate(
                    "process.exec",
                    {
                        "resource": "/workspace/package-probe",
                        "boundary": "range",
                        "escape_sequence": "synthetic-boundary-confusion",
                    },
                )
            assert raised.value.original_tier == "block"
            assert raised.value.score == 80
        finally:
            guard.shutdown()

    def test_control_plane_pivot_requires_approval_and_fails_closed(self):
        guard = self._guard()
        try:
            with pytest.raises(ActionBlockedError) as raised:
                guard.validate(
                    "service.call",
                    {
                        "destination": "artifact-proxy.mock",
                        "target": "package-control.mock",
                    },
                )
            assert raised.value.original_tier == "intervene"
            assert raised.value.score == 45
        finally:
            guard.shutdown()
