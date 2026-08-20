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
                "runtime_state_sequences",
                "audit_chain_heads",
            } <= tables
            assert con.execute(
                "SELECT count(*) FROM audit_chain_heads WHERE name='global'"
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
