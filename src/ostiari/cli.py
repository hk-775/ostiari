"""Ostiari CLI — command-line interface for agent safety management."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import click

from ostiari import __version__


def _parse_duration(value: str) -> timedelta:
    match = re.match(r"^(\d+)([smhd])$", value.strip())
    if not match:
        raise click.BadParameter(f"Invalid duration format: {value!r}. Use e.g. 1h, 7d, 30m")
    amount, unit = int(match.group(1)), match.group(2)
    mapping = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    return timedelta(**{mapping[unit]: amount})


def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


@click.group()
@click.version_option(version=__version__, prog_name="ostiari")
def main() -> None:
    """Ostiari — Runtime safety and reliability layer for AI agents."""


@main.command("check")
def check_cmd() -> None:
    """Run health checks (storage, config, Python version)."""
    from ostiari.health import HealthChecker

    checker = HealthChecker()
    result = checker.run()
    click.echo(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "ok" else 1)


@main.command()
@click.option("--path", type=click.Path(), default=None, help="Target directory")
def init(path: str | None) -> None:
    """Initialize Ostiari config directory with defaults."""
    from pathlib import Path

    target = Path(path) if path else Path.cwd()
    ag_dir = target / ".ostiari"

    if ag_dir.exists():
        click.echo(f"Warning: {ag_dir} already exists. Skipping.")
        return

    ag_dir.mkdir(parents=True)
    (ag_dir / "config.yaml").write_text(
        "# Ostiari Configuration\n"
        "fail_open: true\n"
        "log_level: INFO\n"
        "thresholds:\n"
        "  allow_max: 30\n"
        "  intervene_max: 70\n"
    )
    policies_dir = ag_dir / "policies"
    policies_dir.mkdir()
    (policies_dir / "default.yaml").write_text(
        "# Example policy\n"
        "rules:\n"
        "  - type: block\n"
        '    action: "*.delete_database"\n'
        "    description: Prevent database deletion\n"
    )
    click.echo(f"Initialized Ostiari at {ag_dir}")


@main.command()
@click.argument("action")
@click.option("--params", type=str, default=None, help="JSON-encoded parameters")
def validate(action: str, params: str | None) -> None:
    """Run a single action validation."""
    from ostiari import Guard

    parsed_params: dict[str, Any] = {}
    if params:
        try:
            parsed_params = json.loads(params)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON params: {e}", err=True)
            raise SystemExit(2) from None

    guard = Guard()
    try:
        result = guard.validate(action, parsed_params)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2) from None

    if _is_tty():
        colors = {"allow": "green", "intervene": "yellow", "block": "red"}
        color = colors.get(result.tier, "white")
        click.echo(
            click.style(f"[{result.tier.upper()}]", fg=color)
            + f" {result.action} score={result.score} duration={result.duration_ms:.1f}ms"
        )
        if result.signals:
            for sig in result.signals:
                click.echo(
                    f"  signal: {sig.source} ({sig.score_contribution:+d}) {sig.description}"
                )
    else:
        click.echo(json.dumps(result.model_dump(mode="json")))

    raise SystemExit(0 if result.tier != "block" else 1)


@main.command()
@click.option("--limit", type=int, default=20, help="Number of traces to show")
@click.option("--action", "action_filter", type=str, default=None, help="Filter by action pattern")
@click.option("--tier", type=click.Choice(["allow", "intervene", "block"]), default=None)
@click.option("--since", type=str, default=None, help="Duration filter (e.g. 1h, 7d)")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default=None)
def traces(
    limit: int,
    action_filter: str | None,
    tier: str | None,
    since: str | None,
    fmt: str | None,
) -> None:
    """Query and display execution traces."""
    from ostiari.models import TraceFilters
    from ostiari.storage import SQLiteBackend

    output_format = fmt or ("table" if _is_tty() else "json")

    start_time = None
    if since:
        try:
            delta = _parse_duration(since)
            start_time = datetime.now(timezone.utc) - delta
        except click.BadParameter as e:
            click.echo(str(e), err=True)
            raise SystemExit(2) from None

    storage = SQLiteBackend()
    try:
        filters = TraceFilters(
            start_time=start_time,
            action=action_filter,
            tier=tier,
            limit=limit,
        )
        results = storage.get_traces(filters)
    finally:
        storage.close()

    if not results:
        click.echo("No traces found.")
        return

    if output_format == "json":
        click.echo(json.dumps([t.model_dump(mode="json") for t in results], indent=2))
    else:
        _print_traces_table(results)


def _print_traces_table(traces: list[Any]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for t in traces:
            click.echo(
                f"{t.timestamp.isoformat()} {t.action:30s} {t.risk_score:3d} {t.tier:10s} {t.duration_ms:.0f}ms"
            )
        return

    table = Table(title="Execution Traces")
    table.add_column("Time", style="dim")
    table.add_column("Action")
    table.add_column("Score", justify="right")
    table.add_column("Tier")
    table.add_column("Duration", justify="right")

    tier_colors = {"allow": "green", "intervene": "yellow", "block": "red"}
    for t in traces:
        table.add_row(
            t.timestamp.strftime("%H:%M:%S"),
            t.action,
            str(t.risk_score),
            f"[{tier_colors.get(t.tier, 'white')}]{t.tier}[/]",
            f"{t.duration_ms:.0f}ms",
        )

    Console().print(table)


@main.command()
@click.option("--db", type=click.Path(), default=None, help="Database path")
def tui(db: str | None) -> None:
    """Launch the terminal monitoring UI."""
    try:
        from ostiari.tui.app import OstiariApp
    except ImportError:
        click.echo("TUI requires 'textual'. Install with: pip install ostiari[tui]")
        raise SystemExit(1) from None

    app = OstiariApp(db_path=db)
    app.run()


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", default=8420, type=int, help="Bind port")
@click.option("--no-browser", is_flag=True, help="Don't open browser on start")
def dashboard(host: str, port: int, no_browser: bool) -> None:
    """Launch the web monitoring dashboard."""
    try:
        import uvicorn

        from ostiari.dashboard.app import create_app
    except ImportError:
        click.echo("Dashboard requires: pip install ostiari[dashboard]")
        raise SystemExit(1) from None

    app = create_app()
    if not no_browser and host in ("127.0.0.1", "localhost"):
        import webbrowser

        webbrowser.open(f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", default=8421, type=int, help="Bind port")
@click.option("--policy", default="policy.yaml", type=click.Path(), help="Policy file path")
@click.option("--tools", default=None, type=click.Path(), help="Tool registry YAML config")
def proxy(host: str, port: int, policy: str, tools: str | None) -> None:
    """Run the sidecar proxy server (validate + execute tool calls)."""
    try:
        from ostiari.proxy import run_proxy
    except ImportError:
        click.echo("Proxy requires: pip install ostiari[dashboard]")
        raise SystemExit(1) from None

    click.echo(f"Ostiari Proxy: http://{host}:{port}")
    click.echo("  POST /tool/{action}  — validate & execute")
    click.echo("  POST /validate        — validate only")
    click.echo("  GET  /tools           — list registered tools")
    click.echo("  GET  /health          — health check")
    run_proxy(policy_path=policy, tools_config=tools, host=host, port=port)


@main.command()
@click.option("--period", type=int, default=7, help="Report period in days")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json")
@click.option("--output", type=click.Path(), default=None, help="Output file (default: stdout)")
def report(period: int, fmt: str, output: str | None) -> None:
    """Generate a compliance report."""
    from ostiari.report import ReportGenerator
    from ostiari.storage import SQLiteBackend

    storage = SQLiteBackend()
    try:
        generator = ReportGenerator(storage)
        data = generator.generate(period_days=period, format=fmt)
    finally:
        storage.close()

    if output:
        from pathlib import Path

        Path(output).write_bytes(data)
        click.echo(f"Report written to {output}")
    else:
        click.echo(data.decode("utf-8"))
