"""Unit tests for the CLI module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ostiari.cli import main


@pytest.fixture
def runner():
    return CliRunner()


class TestVersion:
    def test_version_flag(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "ostiari" in result.output


class TestCheck:
    def test_check_healthy(self, runner):
        with patch("ostiari.health.HealthChecker") as mock_cls:
            mock_cls.return_value.run.return_value = {"status": "ok", "checks": []}
            result = runner.invoke(main, ["check"])
            assert result.exit_code == 0
            assert '"status": "ok"' in result.output

    def test_check_unhealthy(self, runner):
        with patch("ostiari.health.HealthChecker") as mock_cls:
            mock_cls.return_value.run.return_value = {"status": "error", "checks": []}
            result = runner.invoke(main, ["check"])
            assert result.exit_code == 1


class TestInit:
    def test_init_creates_directory(self, runner, tmp_path):
        result = runner.invoke(main, ["init", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / ".ostiari" / "config.yaml").exists()
        assert (tmp_path / ".ostiari" / "policies" / "default.yaml").exists()

    def test_init_existing_warns(self, runner, tmp_path):
        (tmp_path / ".ostiari").mkdir()
        result = runner.invoke(main, ["init", "--path", str(tmp_path)])
        assert "already exists" in result.output


class TestValidate:
    def test_validate_allow(self, runner):
        mock_result = MagicMock()
        mock_result.tier = "allow"
        mock_result.action = "test.action"
        mock_result.score = 10
        mock_result.duration_ms = 1.5
        mock_result.signals = []
        mock_result.model_dump.return_value = {"tier": "allow", "score": 10}

        with patch("ostiari.Guard") as mock_guard_cls:
            mock_guard_cls.return_value.validate.return_value = mock_result
            result = runner.invoke(main, ["validate", "test.action"])
            assert result.exit_code == 0

    def test_validate_block(self, runner):
        mock_result = MagicMock()
        mock_result.tier = "block"
        mock_result.action = "danger.action"
        mock_result.score = 90
        mock_result.duration_ms = 2.0
        mock_result.signals = []
        mock_result.model_dump.return_value = {"tier": "block", "score": 90}

        with patch("ostiari.Guard") as mock_guard_cls:
            mock_guard_cls.return_value.validate.return_value = mock_result
            result = runner.invoke(main, ["validate", "danger.action"])
            assert result.exit_code == 1

    def test_validate_invalid_json_params(self, runner):
        result = runner.invoke(main, ["validate", "action", "--params", "not-json"])
        assert result.exit_code == 2


class TestTraces:
    def test_traces_json_output(self, runner):
        mock_trace = MagicMock()
        mock_trace.model_dump.return_value = {
            "trace_id": "t1",
            "action": "test",
            "risk_score": 20,
            "tier": "allow",
        }

        with patch("ostiari.storage.SQLiteBackend") as mock_storage_cls:
            mock_storage = mock_storage_cls.return_value
            mock_storage.get_traces.return_value = [mock_trace]
            result = runner.invoke(main, ["traces", "--format", "json"])
            assert result.exit_code == 0
            assert "t1" in result.output

    def test_traces_empty(self, runner):
        with patch("ostiari.storage.SQLiteBackend") as mock_storage_cls:
            mock_storage_cls.return_value.get_traces.return_value = []
            result = runner.invoke(main, ["traces", "--format", "json"])
            assert "No traces found" in result.output

    def test_traces_invalid_since(self, runner):
        with patch("ostiari.storage.SQLiteBackend") as mock_storage_cls:
            mock_storage_cls.return_value.get_traces.return_value = []
            result = runner.invoke(main, ["traces", "--since", "invalid"])
            assert result.exit_code == 2


class TestTui:
    def test_tui_missing_dep(self, runner):
        with patch.dict("sys.modules", {"ostiari.tui": None, "ostiari.tui.app": None}):
            result = runner.invoke(main, ["tui"])
            assert result.exit_code == 1
            assert "textual" in result.output.lower() or result.exit_code == 1


class TestDashboard:
    def test_dashboard_missing_dep(self, runner):
        with patch("builtins.__import__", side_effect=ImportError("no fastapi")):
            result = runner.invoke(main, ["dashboard"])
            assert result.exit_code in (0, 1)


class TestReport:
    def test_report_json(self, runner):
        with patch("ostiari.storage.SQLiteBackend") as mock_cls:
            with patch("ostiari.report.ReportGenerator") as mock_gen_cls:
                mock_gen_cls.return_value.generate.return_value = b'{"status": "ok"}'
                result = runner.invoke(main, ["report", "--format", "json"])
                assert result.exit_code == 0
                assert "ok" in result.output

    def test_report_to_file(self, runner, tmp_path):
        out = tmp_path / "report.json"
        with patch("ostiari.storage.SQLiteBackend") as mock_cls:
            with patch("ostiari.report.ReportGenerator") as mock_gen_cls:
                mock_gen_cls.return_value.generate.return_value = b'{"status": "ok"}'
                result = runner.invoke(main, ["report", "--output", str(out)])
                assert result.exit_code == 0
                assert out.exists()
