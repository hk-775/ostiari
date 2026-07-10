"""Unit tests for ostiari.storage.migrations."""

import sqlite3

import pytest

from ostiari.storage.migrations import discover_migrations, run_migrations


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


class TestDiscoverMigrations:
    def test_finds_initial_migration(self):
        migrations = discover_migrations()
        assert len(migrations) >= 1
        assert migrations[0].version == 1
        assert migrations[0].description != ""

    def test_sorted_by_version(self):
        migrations = discover_migrations()
        versions = [m.version for m in migrations]
        assert versions == sorted(versions)


class TestRunMigrations:
    def test_creates_schema_version_table(self, conn):
        run_migrations(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        assert row is not None

    def test_creates_traces_table(self, conn):
        run_migrations(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='traces'"
        ).fetchone()
        assert row is not None

    def test_creates_checkpoints_table(self, conn):
        run_migrations(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone()
        assert row is not None

    def test_creates_breaker_states_table(self, conn):
        run_migrations(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='breaker_states'"
        ).fetchone()
        assert row is not None

    def test_records_version(self, conn):
        version = run_migrations(conn)
        assert version == 1
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] == 1

    def test_idempotent(self, conn):
        run_migrations(conn)
        version = run_migrations(conn)
        assert version == 1

    def test_creates_indexes(self, conn):
        run_migrations(conn)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        index_names = {row["name"] for row in indexes}
        assert "idx_traces_timestamp" in index_names
        assert "idx_traces_action" in index_names
        assert "idx_traces_tier" in index_names
        assert "idx_traces_correlation" in index_names
        assert "idx_checkpoints_name" in index_names
        assert "idx_checkpoints_sequence" in index_names
