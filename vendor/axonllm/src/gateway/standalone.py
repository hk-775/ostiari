"""Fail-closed standalone HTTP host for container deployments."""

from __future__ import annotations

import os
from typing import Any

import uvicorn

from src.gateway.bootstrap import build_starlette_app
from src.gateway.config import AppConfig
from src.gateway.config_loader import load_app_config

_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 30
_MAX_GRACEFUL_SHUTDOWN_SECONDS = 120


def _graceful_shutdown_seconds(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> int:
    source = os.environ if environ is None else environ
    raw = source.get(
        "AXON_GRACEFUL_SHUTDOWN_SECONDS",
        str(_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("AXON_GRACEFUL_SHUTDOWN_SECONDS must be an integer") from exc
    if not 1 <= value <= _MAX_GRACEFUL_SHUTDOWN_SECONDS:
        raise ValueError("AXON_GRACEFUL_SHUTDOWN_SECONDS must be between 1 and 120")
    return value


def build_app() -> tuple[Any, AppConfig]:
    """Build the combined gateway, control API, and UI without dev defaults."""

    app_config = load_app_config()
    return build_starlette_app(app_config), app_config


def main() -> None:
    """Run the standalone host until SIGINT or SIGTERM."""

    app, app_config = build_app()
    uvicorn.run(
        app,
        host=app_config.server_host,
        port=app_config.server_port,
        timeout_graceful_shutdown=_graceful_shutdown_seconds(),
    )


if __name__ == "__main__":
    main()
