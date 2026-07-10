"""Unit tests for the HealthChecker module."""

from __future__ import annotations

from unittest.mock import MagicMock

from ostiari.health import HealthChecker


class TestHealthChecker:
    def test_all_checks_pass(self):
        mock_storage = MagicMock()
        mock_storage.schema_version.return_value = 3

        checker = HealthChecker(storage=mock_storage)
        result = checker.run()

        assert result["status"] == "ok"
        assert len(result["checks"]) == 3
        assert all(c["status"] == "ok" for c in result["checks"])

    def test_storage_failure(self):
        mock_storage = MagicMock()
        mock_storage.schema_version.side_effect = RuntimeError("db locked")

        checker = HealthChecker(storage=mock_storage)
        result = checker.run()

        assert result["status"] == "error"
        storage_check = next(c for c in result["checks"] if c["name"] == "storage")
        assert storage_check["status"] == "error"
        assert "db locked" in storage_check["error"]

    def test_python_version_check(self):
        checker = HealthChecker(storage=MagicMock())
        result = checker.run()

        python_check = next(c for c in result["checks"] if c["name"] == "python")
        assert python_check["status"] == "ok"
        assert "version" in python_check

    def test_storage_version_in_result(self):
        mock_storage = MagicMock()
        mock_storage.schema_version.return_value = 5

        checker = HealthChecker(storage=mock_storage)
        result = checker.run()

        storage_check = next(c for c in result["checks"] if c["name"] == "storage")
        assert storage_check["version"] == 5
