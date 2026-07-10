"""Ostiari health checker — diagnostic checks for all subsystems."""

from __future__ import annotations

import sys
from typing import Any


class HealthChecker:
    """Runs diagnostic checks for storage, config, and runtime environment."""

    def __init__(self, storage: Any = None, config_path: str | None = None) -> None:
        self._storage = storage
        self._config_path = config_path

    def run(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        checks.append(self._check_storage())
        checks.append(self._check_config())
        checks.append(self._check_python())

        all_ok = all(c["status"] == "ok" for c in checks)
        return {"status": "ok" if all_ok else "error", "checks": checks}

    def _check_storage(self) -> dict[str, Any]:
        try:
            if self._storage is None:
                from ostiari.storage import SQLiteBackend

                storage = SQLiteBackend()
            else:
                storage = self._storage

            version = storage.schema_version()

            if self._storage is None:
                storage.close()

            return {"name": "storage", "status": "ok", "version": version}
        except Exception as e:
            return {"name": "storage", "status": "error", "error": str(e)}

    def _check_config(self) -> dict[str, Any]:
        try:
            from ostiari.config import ConfigLoader

            ConfigLoader.load(path=self._config_path)
            return {"name": "config", "status": "ok"}
        except Exception as e:
            return {"name": "config", "status": "error", "error": str(e)}

    def _check_python(self) -> dict[str, Any]:
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        return {"name": "python", "status": "ok", "version": version}
