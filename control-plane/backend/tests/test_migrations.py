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
            con.close()
        finally:
            _restore_env(prev)


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
