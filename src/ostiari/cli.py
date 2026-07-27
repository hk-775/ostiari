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


@main.command("import-openapi")
@click.argument("spec")
@click.option("--gateway", default="http://localhost:8421", help="Ostiari gateway base URL")
@click.option("--server-url", default=None, help="Override the API base URL from the spec")
@click.option("--name-prefix", default="", help="Prefix for generated tool names (e.g. 'crm.')")
@click.option("--replace", is_flag=True, help="Replace all existing tools instead of merging")
@click.option("--preview", is_flag=True, help="Show the tools that would be generated, don't register")
def import_openapi(
    spec: str, gateway: str, server_url: str | None, name_prefix: str,
    replace: bool, preview: bool,
) -> None:
    """Generate governed tools from an OpenAPI SPEC (URL, file, or JSON/YAML).

    Each REST operation becomes an Ostiari tool that inherits the full gate
    chain. SPEC may be an http(s) URL, a local file path, or literal JSON/YAML.
    """
    from pathlib import Path

    import httpx

    # A URL is forwarded as-is (the gateway fetches it); a readable file is
    # inlined; anything else is treated as a literal spec string.
    payload: dict[str, Any] = {
        "server_url": server_url, "name_prefix": name_prefix,
        "replace": replace, "preview": preview,
    }
    if re.match(r"^https?://", spec.strip()):
        payload["source"] = spec.strip()
    elif Path(spec).is_file():
        payload["source"] = Path(spec).read_text(encoding="utf-8")
    else:
        payload["source"] = spec

    try:
        resp = httpx.post(f"{gateway.rstrip('/')}/config/tools/import-openapi",
                          json=payload, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        click.echo(f"Error: could not reach gateway {gateway}: {e}", err=True)
        raise SystemExit(2) from None

    if resp.status_code != 200:
        click.echo(f"Error: {resp.status_code} {resp.text[:300]}", err=True)
        raise SystemExit(1)

    data = resp.json()
    tools = data.get("tools", [])
    if _is_tty():
        verb = "Would generate" if preview else "Imported"
        click.echo(click.style(f"{verb} {data.get('count', len(tools))} tool(s)", fg="green"))
        for t in tools:
            loc = []
            if t.get("path_params"):
                loc.append(f"path={t['path_params']}")
            if t.get("query_params"):
                loc.append(f"query={t['query_params']}")
            suffix = ("  " + " ".join(loc)) if loc else ""
            click.echo(f"  {t['method']:6} {t['name']:24} → {t['endpoint']}{suffix}")
    else:
        click.echo(json.dumps(data))
    raise SystemExit(0)


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


@main.group()
def shadow() -> None:
    """Shadow mode — run policies in observe-only mode and report would-blocks."""


@shadow.command("run")
@click.option("--gateway", "gateway_id", required=True, help="Gateway ID to run in shadow mode")
@click.option("--control-plane", default="http://localhost:8400", help="Control plane base URL")
@click.option("--duration", default=None, help="How long to stay in shadow mode, e.g. 30m, 14d (omit = set-and-report now)")
@click.option("--restore/--no-restore", default=True, help="Restore enforce mode after the run (default: restore)")
def shadow_run(gateway_id: str, control_plane: str, duration: str | None, restore: bool) -> None:
    """Put a gateway in shadow mode, optionally wait, then print the shadow report.

    "Try before you enforce": routes real traffic through policy evaluation
    without blocking, then shows what enforce mode WOULD have blocked.
    """
    try:
        import httpx
    except ImportError:
        click.echo("Error: httpx is required for 'shadow run' (pip install httpx).", err=True)
        raise SystemExit(1) from None

    base = control_plane.rstrip("/")
    wait = _parse_duration(duration).total_seconds() if duration else 0

    with httpx.Client(timeout=10.0) as client:
        # 1. Switch to shadow mode
        r = client.put(f"{base}/api/gateways/{gateway_id}/mode", json={"mode": "shadow"})
        if r.status_code == 404:
            click.echo(f"Error: gateway '{gateway_id}' not found.", err=True)
            raise SystemExit(1)
        r.raise_for_status()
        click.echo(click.style(f"● Gateway '{gateway_id}' is now in SHADOW mode.", fg="yellow"))

        # 2. Optionally wait while traffic flows
        if wait:
            click.echo(f"  Observing for {duration} (Ctrl-C to stop early)...")
            try:
                import time
                time.sleep(wait)
            except KeyboardInterrupt:
                click.echo("  Interrupted — reporting now.")

        # 3. Fetch and print the report
        rep = client.get(f"{base}/api/traces/shadow-report").json()
        click.echo("")
        click.echo(click.style("Shadow Report", bold=True))
        click.echo(f"  Shadow calls : {rep['total_shadow_calls']}")
        click.echo(f"  Would block  : {click.style(str(rep['would_block_count']), fg='red')}")
        click.echo(f"  Would allow  : {click.style(str(rep['would_allow_count']), fg='green')}")
        click.echo(f"  Block rate   : {round(rep['block_rate'] * 100)}%")
        if rep["offending_actions"]:
            click.echo("  Actions that would be blocked:")
            for a in rep["offending_actions"]:
                reasons = ", ".join(a["reasons"]) or "—"
                click.echo(f"    - {a['action']}  (x{a['count']}, max risk {a['max_score']}) — {reasons}")

        # 4. Restore enforce mode
        if restore:
            client.put(f"{base}/api/gateways/{gateway_id}/mode", json={"mode": "enforce"})
            click.echo(click.style(f"\n● Gateway '{gateway_id}' restored to ENFORCE mode.", fg="green"))
        else:
            click.echo(click.style(f"\n● Gateway '{gateway_id}' left in SHADOW mode.", fg="yellow"))


@main.group()
def compliance() -> None:
    """Compliance — generate auditor-ready reports from governance data."""


@compliance.command("report")
@click.option("--framework", default="eu-ai-act", help="Compliance framework (e.g. eu-ai-act)")
@click.option("--control-plane", default="http://localhost:8400", help="Control plane base URL")
@click.option("--period", "period_days", default=90, type=int, help="Reporting window in days")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON instead of a summary")
def compliance_report(framework: str, control_plane: str, period_days: int, as_json: bool) -> None:
    """Generate and print a compliance report."""
    try:
        import httpx
    except ImportError:
        click.echo("Error: httpx is required for 'compliance report' (pip install httpx).", err=True)
        raise SystemExit(1) from None

    base = control_plane.rstrip("/")
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{base}/api/compliance/report",
                       params={"framework": framework, "period_days": period_days})
        if r.status_code == 400:
            click.echo(f"Error: unknown framework '{framework}'.", err=True)
            raise SystemExit(1)
        r.raise_for_status()
        rep = r.json()

    if as_json:
        click.echo(json.dumps(rep, indent=2))
        return

    posture_color = {"green": "green", "yellow": "yellow", "red": "red"}.get(rep["posture"], "white")
    click.echo(click.style(f"Compliance Report — {rep['framework']} ({period_days}d)", bold=True))
    click.echo("  Posture : " + click.style(rep["posture"].upper(), fg=posture_color)
               + f"  ({rep['score_pct']}% requirements met)")
    ev = rep["evidence"]
    click.echo(f"  Evidence: {ev['policy_count']} policies · {ev['audit_count']} audit records · "
               f"{ev['trace_count']} traces · {ev['blocked_count']} blocked · "
               f"{ev['intervene_count']} human-oversight")
    click.echo("")
    marks = {"met": ("✓", "green"), "partial": ("~", "yellow"), "unmet": ("✗", "red")}
    for req in rep["requirements"]:
        mark, color = marks.get(req["status"], ("?", "white"))
        click.echo("  " + click.style(mark, fg=color) + f" {req['title']}")
        click.echo(f"      {req['detail']}")


@main.command("metering")
@click.option("--group-by", type=click.Choice(["agent", "gateway", "tool"]), default="agent")
@click.option("--control-plane", default="http://localhost:8400", help="Control plane base URL")
@click.option("--period", "period_days", default=30, type=int, help="Reporting window in days")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON")
def metering_cmd(group_by: str, control_plane: str, period_days: int, as_json: bool) -> None:
    """Show governed tool-call metering (usage-based billing lens)."""
    try:
        import httpx
    except ImportError:
        click.echo("Error: httpx is required for 'metering' (pip install httpx).", err=True)
        raise SystemExit(1) from None

    base = control_plane.rstrip("/")
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{base}/api/metering/summary",
                       params={"group_by": group_by, "period_days": period_days})
        r.raise_for_status()
        s = r.json()

    if as_json:
        click.echo(json.dumps(s, indent=2))
        return

    click.echo(click.style(f"Metering — by {s['group_by']} ({period_days}d)", bold=True))
    click.echo(f"  {s['total_governed_calls']:,} governed calls · "
               f"{s['distinct_subjects']} {s['group_by']}s · "
               f"overall tier: " + click.style(s['overall_tier'].upper(), fg="cyan"))
    click.echo("")
    for row in s["breakdown"]:
        nxt = row.get("next_tier")
        tail = f"  ({nxt['calls_to_next']:,} → {nxt['tier']})" if nxt else ""
        click.echo(f"  {row['key']:24s} {row['calls']:>8,} calls  [{row['tier']}]{tail}")


@main.group()
def wallet() -> None:
    """Agent wallets — x402 balances, funding, and spend (pay-per-tool-call)."""


def _wallet_client(control_plane: str) -> tuple[Any, str]:
    """Return (httpx module, normalized base URL) — httpx is an optional extra."""
    try:
        import httpx
    except ImportError:
        click.echo("Error: httpx is required for 'wallet' (pip install httpx).", err=True)
        raise SystemExit(1) from None
    return httpx, control_plane.rstrip("/")


@wallet.command("list")
@click.option("--control-plane", default="http://localhost:8400", help="Control plane base URL")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON")
def wallet_list(control_plane: str, as_json: bool) -> None:
    """List agent wallets and balances."""
    httpx, base = _wallet_client(control_plane)
    with httpx.Client(timeout=10.0) as client:
        wallets = client.get(f"{base}/api/payments/wallets").json()
    if as_json:
        click.echo(json.dumps(wallets, indent=2))
        return
    if not wallets:
        click.echo("No wallets provisioned.")
        return
    click.echo(click.style("Agent Wallets", bold=True))
    for w in wallets:
        status_color = "green" if w["status"] == "active" else "red"
        limit = f" · daily ${w['daily_limit_usdc']:.2f}" if w["daily_limit_usdc"] is not None else ""
        click.echo(
            f"  {w['agent_id']:24s} ${w['balance_usdc']:>8.4f}  "
            + click.style(w["status"], fg=status_color)
            + f"  (spent today ${w['spent_today_usdc']:.4f}{limit})"
        )


@wallet.command("fund")
@click.option("--agent", required=True, help="Agent ID whose wallet to fund")
@click.option("--amount", required=True, type=float, help="USDC amount to deposit")
@click.option("--control-plane", default="http://localhost:8400", help="Control plane base URL")
def wallet_fund(agent: str, amount: float, control_plane: str) -> None:
    """Deposit USDC into an agent wallet."""
    httpx, base = _wallet_client(control_plane)
    with httpx.Client(timeout=10.0) as client:
        r = client.post(f"{base}/api/payments/wallets/{agent}/fund", json={"amount_usdc": amount})
        if r.status_code == 404:
            click.echo(f"Error: wallet for '{agent}' not found.", err=True)
            raise SystemExit(1)
        r.raise_for_status()
        w = r.json()
    click.echo(click.style(f"● Funded {agent}: +${amount:.4f} → ${w['balance_usdc']:.4f}", fg="green"))


@wallet.command("ledger")
@click.option("--agent", default=None, help="Filter by agent ID")
@click.option("--control-plane", default="http://localhost:8400", help="Control plane base URL")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON")
def wallet_ledger(agent: str | None, control_plane: str, as_json: bool) -> None:
    """Show the payment transaction ledger."""
    httpx, base = _wallet_client(control_plane)
    params = {"agent_id": agent} if agent else {}
    with httpx.Client(timeout=10.0) as client:
        rows = client.get(f"{base}/api/payments/ledger", params=params).json()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("No transactions.")
        return
    click.echo(click.style("Payment Ledger", bold=True))
    for p in rows:
        mark, color = ("✓", "green") if p["settled"] else ("✗", "red")
        click.echo("  " + click.style(mark, fg=color)
                   + f" {p['agent_id']:20s} {p['action']:20s} ${p['amount_usdc']:.4f}  {p['tx_hash']}")


if __name__ == "__main__":
    main()
