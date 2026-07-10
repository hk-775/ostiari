"""Initial schema — traces, checkpoints, breaker_states tables."""

from __future__ import annotations

import sqlite3

VERSION = 1
DESCRIPTION = "Initial schema: traces, checkpoints, breaker_states"


def up(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY,
            correlation_id TEXT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            params TEXT NOT NULL,
            result TEXT,
            risk_score INTEGER NOT NULL,
            tier TEXT NOT NULL,
            duration_ms REAL NOT NULL,
            signals TEXT NOT NULL,
            anomalies TEXT NOT NULL,
            breaker_state TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX idx_traces_timestamp ON traces(timestamp);
        CREATE INDEX idx_traces_action ON traces(action);
        CREATE INDEX idx_traces_tier ON traces(tier);
        CREATE INDEX idx_traces_correlation ON traces(correlation_id);

        CREATE TABLE checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            name TEXT,
            sequence_number INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            state TEXT NOT NULL,
            action TEXT NOT NULL,
            params TEXT NOT NULL,
            result TEXT
        );

        CREATE INDEX idx_checkpoints_name ON checkpoints(name);
        CREATE INDEX idx_checkpoints_sequence ON checkpoints(sequence_number);

        CREATE TABLE breaker_states (
            breaker_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            tripped_at TEXT,
            last_checked TEXT NOT NULL,
            metrics TEXT NOT NULL DEFAULT '{}',
            recovery_mode TEXT NOT NULL,
            recovery_after_seconds INTEGER
        );
    """)


def down(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        DROP TABLE IF EXISTS breaker_states;
        DROP TABLE IF EXISTS checkpoints;
        DROP TABLE IF EXISTS traces;
    """)
