"""Schema migration framework for Ostiari storage."""

from __future__ import annotations

import importlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ostiari.exceptions import StorageMigrationError

logger = logging.getLogger("ostiari")


@dataclass
class Migration:
    version: int
    description: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]


def discover_migrations() -> list[Migration]:
    migrations_dir = Path(__file__).parent
    modules: list[Migration] = []
    for file in sorted(migrations_dir.glob("*.py")):
        if file.name == "__init__.py":
            continue
        module = importlib.import_module(f"ostiari.storage.migrations.{file.stem}")
        modules.append(
            Migration(
                version=module.VERSION,
                description=module.DESCRIPTION,
                up=module.up,
                down=module.down,
            )
        )
    return sorted(modules, key=lambda m: m.version)


def run_migrations(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL, "
        "description TEXT)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current_version = row[0] if row[0] is not None else 0

    migrations = discover_migrations()
    pending = [m for m in migrations if m.version > current_version]

    if not pending:
        logger.debug("Schema up to date at version %d", current_version)
        return current_version

    for migration in pending:
        try:
            conn.execute("BEGIN")
            migration.up(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at, description) "
                "VALUES (?, datetime('now'), ?)",
                (migration.version, migration.description),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise StorageMigrationError(
                from_version=current_version,
                to_version=migration.version,
                reason=str(e),
            ) from e
        current_version = migration.version

    logger.info(
        "Migrated schema from v%d to v%d",
        row[0] if row[0] is not None else 0,
        current_version,
    )
    return current_version
