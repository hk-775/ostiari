"""CLI entrypoint for the Ostiari sidecar."""

import json
import logging
from pathlib import Path

import click
import yaml


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8421, type=int, help="Bind port")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Initial config YAML")
@click.option("--control-plane", default=None, help="Control plane URL to poll for config")
@click.option("--sidecar-id", default="sidecar-1", help="Unique sidecar identifier")
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
        import os, re
        if isinstance(obj, str):
            return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), obj)
        if isinstance(obj, dict):
            return {k: _resolve_env_vars(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_resolve_env_vars(i) for i in obj]
        return obj

    initial_config = None
    if config_path:
        path = Path(config_path)
        if path.exists():
            data = yaml.safe_load(path.read_text())
            data = _resolve_env_vars(data)
            data["sidecar_id"] = sidecar_id
            if control_plane:
                data["control_plane_url"] = control_plane
            initial_config = SidecarConfig(**data)
            click.echo(f"Loaded config from {path}")

    if initial_config is None:
        initial_config = SidecarConfig(
            sidecar_id=sidecar_id,
            control_plane_url=control_plane or "",
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
