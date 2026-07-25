"""CLI entrypoint for the Ostiari sidecar."""

import logging
from pathlib import Path

import click
import yaml


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8421, type=int, envvar="OSTIARI_PORT", help="Bind port")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Initial config YAML")
@click.option(
    "--control-plane", default=None, envvar="OSTIARI_CONTROL_PLANE_URL",
    help="Control plane URL to poll for config",
)
@click.option(
    "--sidecar-id", default="sidecar-1", envvar="OSTIARI_GATEWAY_ID",
    help="Unique sidecar identifier",
)
@click.option("--log-level", default="info", type=click.Choice(["debug", "info", "warning", "error"]))
def cli(
    host: str,
    port: int,
    config_path: str | None,
    control_plane: str | None,
    sidecar_id: str,
    log_level: str,
) -> None:
    """Run the Ostiari generic sidecar proxy."""
    import uvicorn

    from ostiari_gateway.models import SidecarConfig
    from ostiari_gateway.server import create_app

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    def _resolve_env_vars(obj):
        """Recursively resolve ${ENV_VAR} references in config values."""
        import os
        import re
        if isinstance(obj, str):
            return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), obj)
        if isinstance(obj, dict):
            return {k: _resolve_env_vars(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_resolve_env_vars(i) for i in obj]
        return obj

    # Callback URL the control plane uses to push config to THIS gateway.
    # 0.0.0.0 is a bind address, not reachable — advertise localhost for the
    # host so a control plane on the same box can reach us; override with
    # OSTIARI_ADVERTISE_HOST for cross-host deployments (e.g. a k8s service DNS).
    import os as _os
    advertise_host = _os.environ.get("OSTIARI_ADVERTISE_HOST") or (
        "localhost" if host in ("0.0.0.0", "") else host
    )
    callback_url = f"http://{advertise_host}:{port}"

    initial_config = None
    if config_path:
        path = Path(config_path)
        if path.exists():
            data = yaml.safe_load(path.read_text())
            data = _resolve_env_vars(data)
            data["sidecar_id"] = sidecar_id
            if control_plane:
                data["control_plane_url"] = control_plane
            data["callback_url"] = callback_url
            initial_config = SidecarConfig(**data)
            click.echo(f"Loaded config from {path}")

    if initial_config is None:
        initial_config = SidecarConfig(
            sidecar_id=sidecar_id,
            control_plane_url=control_plane or "",
            callback_url=callback_url,
        )

    app = create_app(initial_config=initial_config)

    click.echo(f"Ostiari Sidecar [{sidecar_id}]: http://{host}:{port}")
    click.echo("  POST /tool/{action}       — validate & proxy to remote endpoint")
    click.echo("  POST /validate            — validate only")
    click.echo("  POST /config              — apply full config (control plane)")
    click.echo("  POST /config/tools        — hot-reload tools")
    click.echo("  POST /config/tools/{name} — add/update single tool")
    click.echo("  POST /config/policy       — hot-reload policy")
    click.echo("  GET  /config              — view current config")
    click.echo("  GET  /tools               — list registered tools")
    click.echo("  GET  /health              — health check")

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    cli()
