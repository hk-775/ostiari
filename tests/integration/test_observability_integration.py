"""Integration tests for the observability layer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ostiari.cli import main
from ostiari.health import HealthChecker
from ostiari.models import TraceEntry
from ostiari.report import ReportGenerator
from ostiari.storage import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    db_path = tmp_path / "test.db"
    s = SQLiteBackend(path=str(db_path))
    yield s
    s.close()


def _make_trace(storage, action="test.run", tier="allow", risk_score=20):
    entry = TraceEntry(
        trace_id=f"t-{action}-{tier}-{risk_score}",
        correlation_id="agent-test",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params={"key": "value"},
        result=None,
        risk_score=risk_score,
        tier=tier,
        duration_ms=3.5,
        signals=[],
        anomalies=[],
        breaker_state=None,
        metadata={},
    )
    storage.save_trace(entry)
    return entry


class TestCLIIntegration:
    def test_traces_command_queries_real_db(self, storage):
        _make_trace(storage, "file.read", "allow", 15)
        _make_trace(storage, "file.write", "block", 85)

        runner = CliRunner()
        with patch("ostiari.storage.SQLiteBackend", return_value=storage):
            result = runner.invoke(main, ["traces", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 2


class TestHealthCheckIntegration:
    def test_health_with_real_storage(self, storage):
        checker = HealthChecker(storage=storage)
        result = checker.run()

        assert result["status"] == "ok"
        storage_check = next(c for c in result["checks"] if c["name"] == "storage")
        assert storage_check["status"] == "ok"
        assert storage_check["version"] >= 1


class TestReportIntegration:
    def test_json_report_with_real_storage(self, storage):
        _make_trace(storage, "a.read", "allow", 10)
        _make_trace(storage, "b.delete", "block", 90)
        _make_trace(storage, "c.update", "intervene", 55)

        gen = ReportGenerator(storage)
        data = gen.generate(period_days=1, format="json")
        report = json.loads(data)

        assert report["stats"]["total_actions"] == 3
        assert report["stats"]["allowed"] == 1
        assert report["stats"]["blocked"] == 1
        assert report["stats"]["intervened"] == 1

    def test_csv_report_with_real_storage(self, storage):
        _make_trace(storage, "x.op", "allow", 20)

        gen = ReportGenerator(storage)
        data = gen.generate(period_days=1, format="csv")
        text = data.decode("utf-8")

        lines = text.strip().split("\n")
        assert lines[0].startswith("trace_id,")
        assert len(lines) == 2  # header + 1 row

    def test_empty_report(self, storage):
        gen = ReportGenerator(storage)
        data = gen.generate(period_days=1, format="json")
        report = json.loads(data)

        assert report["status"] == "no_activity"


class TestDashboardIntegration:
    def test_app_creates_successfully(self, storage):
        from ostiari.dashboard.app import create_app

        app = create_app(storage=storage)
        assert app.title == "Ostiari Dashboard"

    @pytest.mark.asyncio
    async def test_health_endpoint(self, storage):
        from httpx import ASGITransport, AsyncClient

        from ostiari.dashboard.app import create_app

        app = create_app(storage=storage)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_traces_endpoint(self, storage):
        from httpx import ASGITransport, AsyncClient

        from ostiari.dashboard.app import create_app

        _make_trace(storage, "test.op", "allow", 25)
        app = create_app(storage=storage)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/traces")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["data"]) == 1
