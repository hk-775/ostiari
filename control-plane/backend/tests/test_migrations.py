"""Alembic migration smoke test — the migrations apply and reverse for real.

Runs the full migration chain against a throwaway SQLite file (synchronous
driver, since alembic's offline/online split and our env.py both resolve the
URL from DATABASE_URL). Proves organizations + org_id land, and that a downgrade
to base tears the schema back down.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _alembic_cfg(db_path: str) -> Config:
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    # Point env.py at this scratch DB via the Config URL directly (env.py reads
    # it fresh). We do NOT mutate os.environ["DATABASE_URL"] — that would leak
    # into the shared app engine used by other tests' fixtures.
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    return cfg


def _restore_env(prev: str | None) -> None:
    if prev is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = prev


def _primary_key_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1]
        for row in sorted(
            (row for row in con.execute(f"PRAGMA table_info({table})") if row[5]),
            key=lambda row: row[5],
        )
    ]


def _unique_column_sets(
    con: sqlite3.Connection,
    table: str,
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for row in con.execute(f"PRAGMA index_list({table})"):
        if not row[2]:
            continue
        columns = tuple(
            info[2]
            for info in con.execute(f"PRAGMA index_info({row[1]})")
        )
        result.add(columns)
    return result


def _foreign_key_column_sets(
    con: sqlite3.Connection,
    table: str,
) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    grouped: dict[int, list[tuple[int, str, str]]] = {}
    for row in con.execute(f"PRAGMA foreign_key_list({table})"):
        grouped.setdefault(row[0], []).append((row[1], row[3], row[4]))
    return {
        (
            tuple(item[1] for item in sorted(items)),
            tuple(item[2] for item in sorted(items)),
        )
        for items in grouped.values()
    }


def test_upgrade_head_creates_org_schema():
    prev = os.environ.get("DATABASE_URL")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "m.db")
        cfg = _alembic_cfg(db)
        try:
            command.upgrade(cfg, "head")
            con = sqlite3.connect(db)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "organizations" in tables
            # default org seeded
            assert con.execute("SELECT count(*) FROM organizations WHERE id='default'").fetchone()[0] == 1
            # org_id present on core tables
            for t in ("gateways", "tools", "policies", "wallets", "usage_records", "audit_logs", "users"):
                cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
                assert "org_id" in cols, f"{t} missing org_id"
            gateway_cols = {
                r[1] for r in con.execute("PRAGMA table_info(gateways)")
            }
            assert {"workload_issuer", "workload_subject"} <= gateway_cols
            assert _primary_key_columns(con, "gateways") == ["org_id", "id"]
            assert _primary_key_columns(con, "wallets") == [
                "org_id",
                "agent_id",
            ]
            for table in (
                "tools",
                "policies",
                "mcp_servers",
                "usage_records",
                "a2a_agents",
            ):
                assert (
                    ("org_id", "gateway_id"),
                    ("org_id", "id"),
                ) in _foreign_key_column_sets(con, table)
            assert ("org_id", "email") in _unique_column_sets(con, "users")
            assert ("org_id", "name") in _unique_column_sets(con, "policies")
            assert (
                "org_id",
                "gateway_id",
                "event_id",
            ) in _unique_column_sets(con, "usage_records")
            assert (
                "org_id",
                "gateway_id",
                "event_id",
            ) in _unique_column_sets(con, "payment_records")
            gateway_indexes = {
                row[1]
                for row in con.execute("PRAGMA index_list(gateways)")
            }
            assert "sqlite_autoindex_gateways_2" in gateway_indexes or any(
                {
                    column[2]
                    for column in con.execute(f"PRAGMA index_info({index_name})")
                }
                == {"workload_issuer", "workload_subject"}
                for index_name in gateway_indexes
            )
            usage_cols = {
                r[1] for r in con.execute("PRAGMA table_info(usage_records)")
            }
            assert {"experiment_name", "experiment_variant"} <= usage_cols
            assert "sandbox_runs" in tables
            assert "provider_routes" in tables
            assert {
                "approval_records",
                "trace_records",
                "sso_login_states",
                "runtime_state_records",
                "runtime_state_revisions",
                "runtime_state_sequences",
                "audit_chain_heads",
            } <= tables
            assert _primary_key_columns(con, "audit_chain_heads") == [
                "org_id",
                "name",
            ]
            assert con.execute(
                "SELECT count(*) FROM audit_chain_heads "
                "WHERE org_id='default' AND name='global'"
            ).fetchone()[0] == 1
            sandbox_cols = {
                r[1] for r in con.execute("PRAGMA table_info(sandbox_runs)")
            }
            assert {
                "org_id",
                "active_slot",
                "gateway_id",
                "source_digest",
                "status",
                "max_output_bytes",
                "max_tool_payload_bytes",
                "tool_calls",
                "completed_at",
            } <= sandbox_cols
            route_cols = {
                r[1] for r in con.execute("PRAGMA table_info(provider_routes)")
            }
            assert {
                "org_id",
                "route_id",
                "provider",
                "endpoint",
                "auth_type",
                "private_config_encrypted",
                "allowed_models",
                "weight",
                "priority",
                "max_concurrency",
                "capacity_group",
                "capacity_limit",
                "max_connections",
                "max_connections_per_host",
                "keepalive_timeout",
            } <= route_cols
            approval_cols = {
                r[1] for r in con.execute("PRAGMA table_info(approval_records)")
            }
            assert {
                "id",
                "org_id",
                "gateway_id",
                "params_encrypted",
                "status",
                "decided_by",
                "decided_at",
            } <= approval_cols
            trace_cols = {
                r[1] for r in con.execute("PRAGMA table_info(trace_records)")
            }
            assert {
                "org_id",
                "trace_id",
                "gateway_id",
                "event",
                "updated_at",
            } <= trace_cols
            con.close()
        finally:
            _restore_env(prev)


def test_tenant_key_migration_preserves_and_rekeys_existing_rows():
    prev = os.environ.get("DATABASE_URL")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "m.db")
        cfg = _alembic_cfg(db)
        try:
            command.upgrade(cfg, "c7d9e1f3a5b7")
            con = sqlite3.connect(db)
            now = "2026-08-21 12:00:00"
            con.execute(
                "INSERT INTO organizations (id, name, created_at) "
                "VALUES ('org-a', 'Org A', ?), ('org-b', 'Org B', ?)",
                (now, now),
            )
            con.execute(
                "INSERT INTO gateways "
                "(id, org_id, name, description, endpoint, status, config, "
                "created_at, updated_at) VALUES "
                "('shared', 'org-a', 'Gateway', '', 'http://gateway:8421', "
                "'registered', '{}', ?, ?)",
                (now, now),
            )
            con.execute(
                "INSERT INTO tools "
                "(org_id, name, endpoint, method, description, timeout_seconds, "
                "gateway_id, created_at) VALUES "
                "(NULL, 'tool', 'http://tool', 'POST', '', 30, 'shared', ?)",
                (now,),
            )
            con.execute(
                "INSERT INTO policies "
                "(org_id, name, description, content, is_active, gateway_id, "
                "created_at, updated_at) VALUES "
                "(NULL, 'shared-policy', '', '{}', 1, 'shared', ?, ?)",
                (now, now),
            )
            con.execute(
                "INSERT INTO usage_records "
                "(org_id, gateway_id, agent_id, model, input_tokens, "
                "output_tokens, total_tokens, cost_usd, action, timestamp, "
                "event_id) VALUES "
                "(NULL, 'shared', 'agent', 'model', 1, 1, 2, 0.1, 'chat', ?, "
                "'usage-event')",
                (now,),
            )
            con.execute(
                "INSERT INTO wallets "
                "(agent_id, org_id, address, balance_usdc, spent_today_usdc, "
                "status, created_at) VALUES "
                "('shared-agent', 'org-a', '', 5, 0, 'active', ?)",
                (now,),
            )
            con.execute(
                "INSERT INTO payment_records "
                "(agent_id, gateway_id, action, amount_usdc, settled, tx_hash, "
                "mode, source, timestamp, org_id, event_id, wallet_debited) "
                "VALUES ('shared-agent', 'shared', 'tool', 0.1, 1, '', "
                "'simulated', 'policy', ?, 'org-a', 'payment-event', 0)",
                (now,),
            )
            con.execute(
                "INSERT INTO users "
                "(org_id, email, name, hashed_password, role, is_active, "
                "created_at) VALUES "
                "('org-a', 'shared@example.com', 'User', 'hash', 'admin', 1, ?)",
                (now,),
            )
            for org in ("org-a", "org-b"):
                con.execute(
                    "INSERT INTO audit_logs "
                    "(org_id, actor, action, resource_type, resource_id, "
                    "details, timestamp) VALUES "
                    "(?, 'admin', 'create', 'gateway', ?, '{}', ?)",
                    (org, f"{org}-gateway", now),
                )
            con.commit()
            con.close()

            command.upgrade(cfg, "head")
            con = sqlite3.connect(db)
            assert con.execute(
                "SELECT org_id FROM tools WHERE gateway_id='shared'"
            ).fetchone() == ("org-a",)
            assert con.execute(
                "SELECT org_id FROM policies WHERE gateway_id='shared'"
            ).fetchone() == ("org-a",)
            assert con.execute(
                "SELECT org_id FROM usage_records WHERE gateway_id='shared'"
            ).fetchone() == ("org-a",)
            assert con.execute(
                "SELECT org_id, entry_hash FROM audit_chain_heads "
                "WHERE name='global' ORDER BY org_id"
            ).fetchall() == [
                ("default", ""),
                ("org-a", con.execute(
                    "SELECT entry_hash FROM audit_logs "
                    "WHERE org_id='org-a'"
                ).fetchone()[0]),
                ("org-b", con.execute(
                    "SELECT entry_hash FROM audit_logs "
                    "WHERE org_id='org-b'"
                ).fetchone()[0]),
            ]

            con.execute(
                "INSERT INTO gateways "
                "(org_id, id, name, description, endpoint, status, config, "
                "created_at, updated_at) VALUES "
                "('org-b', 'shared', 'Gateway B', '', 'http://b:8421', "
                "'registered', '{}', ?, ?)",
                (now, now),
            )
            con.execute(
                "INSERT INTO policies "
                "(org_id, name, description, content, is_active, created_at, "
                "updated_at) VALUES "
                "('org-b', 'shared-policy', '', '{}', 1, ?, ?)",
                (now, now),
            )
            con.execute(
                "INSERT INTO wallets "
                "(org_id, agent_id, address, balance_usdc, spent_today_usdc, "
                "status, created_at) VALUES "
                "('org-b', 'shared-agent', '', 3, 0, 'active', ?)",
                (now,),
            )
            con.execute(
                "INSERT INTO users "
                "(org_id, email, name, hashed_password, role, is_active, "
                "created_at) VALUES "
                "('org-b', 'shared@example.com', 'User B', 'hash', 'admin', 1, ?)",
                (now,),
            )
            con.commit()
            assert con.execute(
                "SELECT count(*) FROM gateways WHERE id='shared'"
            ).fetchone()[0] == 2
            assert con.execute(
                "SELECT count(*) FROM policies WHERE name='shared-policy'"
            ).fetchone()[0] == 2
            assert con.execute(
                "SELECT count(*) FROM wallets WHERE agent_id='shared-agent'"
            ).fetchone()[0] == 2
            assert con.execute(
                "SELECT count(*) FROM users WHERE email='shared@example.com'"
            ).fetchone()[0] == 2
            con.close()
        finally:
            _restore_env(prev)


def test_tenant_key_downgrade_refuses_identifier_collisions():
    prev = os.environ.get("DATABASE_URL")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "m.db")
        cfg = _alembic_cfg(db)
        try:
            command.upgrade(cfg, "head")
            con = sqlite3.connect(db)
            now = "2026-08-21 12:00:00"
            con.execute(
                "INSERT INTO organizations (id, name, created_at) "
                "VALUES ('org-a', 'Org A', ?), ('org-b', 'Org B', ?)",
                (now, now),
            )
            for org in ("org-a", "org-b"):
                con.execute(
                    "INSERT INTO gateways "
                    "(org_id, id, name, description, endpoint, status, config, "
                    "created_at, updated_at) VALUES "
                    "(?, 'shared', 'Gateway', '', 'http://gateway:8421', "
                    "'registered', '{}', ?, ?)",
                    (org, now, now),
                )
            con.commit()
            con.close()

            with pytest.raises(
                RuntimeError,
                match="Cannot downgrade tenant-qualified gateways.id",
            ):
                command.downgrade(cfg, "c7d9e1f3a5b7")
        finally:
            _restore_env(prev)


@pytest.mark.parametrize("table", ["usage_records", "payment_records"])
def test_tenant_key_downgrade_refuses_idempotency_collisions(table):
    prev = os.environ.get("DATABASE_URL")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "m.db")
        cfg = _alembic_cfg(db)
        try:
            command.upgrade(cfg, "head")
            con = sqlite3.connect(db)
            now = "2026-08-21 12:00:00"
            con.execute(
                "INSERT INTO organizations (id, name, created_at) "
                "VALUES ('org-a', 'Org A', ?), ('org-b', 'Org B', ?)",
                (now, now),
            )
            for org, gateway_id in (
                ("org-a", "shared-external-id" if table == "usage_records" else "gateway-a"),
                ("org-b", "shared-external-id" if table == "usage_records" else "gateway-b"),
            ):
                con.execute(
                    "INSERT INTO gateways "
                    "(org_id, id, name, description, endpoint, status, config, "
                    "created_at, updated_at) VALUES "
                    "(?, ?, 'Gateway', '', 'http://gateway:8421', "
                    "'registered', '{}', ?, ?)",
                    (org, gateway_id, now, now),
                )
            if table == "usage_records":
                for org in ("org-a", "org-b"):
                    con.execute(
                        "INSERT INTO usage_records "
                        "(org_id, gateway_id, event_id, agent_id, model, "
                        "input_tokens, output_tokens, total_tokens, cost_usd, "
                        "action, timestamp) VALUES "
                        "(?, 'shared-external-id', 'event-1', 'agent', 'model', "
                        "0, 0, 0, 0, '', ?)",
                        (org, now),
                    )
            else:
                for org in ("org-a", "org-b"):
                    con.execute(
                        "INSERT INTO payment_records "
                        "(org_id, gateway_id, event_id, agent_id, action, "
                        "amount_usdc, settled, tx_hash, mode, source, timestamp) "
                        "VALUES "
                        "(?, 'shared-external-id', 'event-1', 'agent', '', 1, "
                        "1, '', 'simulated', 'policy', ?)",
                        (org, now),
                    )
            con.commit()
            con.close()

            with pytest.raises(
                RuntimeError,
                match=(
                    rf"Cannot downgrade tenant-qualified {table} "
                    "idempotency key"
                ),
            ):
                command.downgrade(cfg, "c7d9e1f3a5b7")
        finally:
            _restore_env(prev)


def test_default_db_path_matches_the_app():
    """alembic and the app must resolve the same default DB.

    When they drift, `alembic upgrade head` with no DATABASE_URL silently migrates
    a *different* file than the app opens: the migration reports success while the
    app still fails on the missing column. Every other test here sets DATABASE_URL,
    so only this one covers it.

    Both now call control_plane.env.default_sqlite_url() instead of each deriving
    the path from its own __file__ (they previously disagreed by one directory
    level). This asserts the single source stays single: a hand-rolled default
    reintroduced in either file fails here.
    """
    backend = Path(__file__).resolve().parent.parent
    for src in (backend / "alembic" / "env.py", backend / "control_plane" / "database.py"):
        text = src.read_text()
        assert "default_sqlite_url()" in text, (
            f"{src.name} no longer derives its default DB from control_plane.env — "
            "migrations and the app can now target different files"
        )
        # The interpolation form is what building one by hand looks like; the bare
        # scheme also appears in prose (database.py's docstring documents it).
        assert "sqlite+aiosqlite:///{" not in text, (
            f"{src.name} builds a SQLite URL by hand again; use "
            "control_plane.env.default_sqlite_url()"
        )


def test_default_sqlite_url_honors_data_dir(tmp_path, monkeypatch):
    """OSTIARI_DATA_DIR relocates the default DB — the read-only-rootfs lever."""
    from control_plane.env import data_dir, default_sqlite_url

    target = tmp_path / "nested" / "data"
    monkeypatch.setenv("OSTIARI_DATA_DIR", str(target))
    assert data_dir() == target
    url = default_sqlite_url()
    assert url == f"sqlite+aiosqlite:///{target / 'control_plane.db'}"
    # Creates the dir (including parents) so SQLite can open the file there.
    assert target.is_dir()


def test_state_file_and_database_share_one_dir(tmp_path, monkeypatch):
    """state.json must sit beside the database, under OSTIARI_DATA_DIR.

    These two were derived from __file__ independently, with a different number of
    .parent hops, so they landed one directory level apart. In the container that
    split was the whole bug: the deploy image only redirects the *database* (via
    DATABASE_URL), so state.json kept resolving relative to the package — a
    root-owned directory the non-root runtime user cannot create. save_state then
    raised PermissionError during lifespan shutdown, which uvicorn reports as
    "Application shutdown failed" *after* the container has already served traffic:
    every restart silently discarded the persisted quotas/experiments/models.
    """
    import subprocess
    import sys

    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = tmp_path / "shared"
    r = subprocess.run(
        [sys.executable, "-c",
         "from control_plane.persistence import STATE_FILE;"
         "from control_plane.database import DATABASE_URL;"
         "print(STATE_FILE); print(DATABASE_URL)"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": backend, "PYTHONDONTWRITEBYTECODE": "1",
             "OSTIARI_DATA_DIR": str(target), "DATABASE_URL": ""},
    )
    assert r.returncode == 0, r.stderr
    state_file, db_url = r.stdout.split()
    assert Path(state_file) == target / "state.json"
    assert db_url.endswith(str(target / "control_plane.db"))
    assert Path(state_file).parent == target, (
        "state.json escaped the data dir; in a container it lands in a root-owned "
        "directory and shutdown loses all in-memory state"
    )


def test_importing_database_writes_nothing(tmp_path):
    """With DATABASE_URL set, importing the app must not write to disk at all.

    This is what readOnlyRootFilesystem depends on. database.py used to mkdir
    unconditionally at import time, resolved from __file__ — inside site-packages
    in a container, unwritable to a non-root user, and raising before any
    application code ran.

    Audits syscalls rather than checking whether one expected path appeared: the
    old code ignored OSTIARI_DATA_DIR and pointed at a directory that already
    exists in a dev checkout, so a path-existence check passes even while the
    container is broken. An audit hook sees the mkdir either way.
    """
    import subprocess
    import sys

    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = """
import sys
writes = []
def hook(event, args):
    if event == "os.mkdir":
        writes.append("mkdir %s" % (args[0],))
    elif event == "open" and len(args) > 1 and args[1] and set(args[1]) & set("wxa+"):
        writes.append("open %s mode=%s" % (args[0], args[1]))
sys.addaudithook(hook)
import control_plane.database   # noqa
import control_plane.persistence  # noqa
if writes:
    sys.stderr.write("import wrote to disk:\\n  " + "\\n  ".join(writes) + "\\n")
    raise SystemExit(1)
"""
    # A subprocess, because these modules are already imported in this process.
    # PYTHONDONTWRITEBYTECODE keeps __pycache__ writes out of the audit trail.
    r = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True,
        env={
            **os.environ,
            "PYTHONPATH": backend,
            "PYTHONDONTWRITEBYTECODE": "1",
            "OSTIARI_DATA_DIR": str(tmp_path / "unused"),
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'x.db'}",
        },
    )
    assert r.returncode == 0, r.stderr


def test_downgrade_base_is_reversible():
    prev = os.environ.get("DATABASE_URL")
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "m.db")
        cfg = _alembic_cfg(db)
        try:
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "base")
            con = sqlite3.connect(db)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Baseline downgrade drops every table it created.
            assert "organizations" not in tables
            assert "gateways" not in tables
            con.close()
        finally:
            _restore_env(prev)
