"""Deployment-environment helpers.

A single, explicit production signal so security-sensitive defaults can be
permissive in dev/demo but fail-closed in production. Set OSTIARI_ENV=production
(or prod) in real deployments.
"""

from __future__ import annotations

import os
from pathlib import Path


def is_production() -> bool:
    """True when OSTIARI_ENV indicates a production deployment."""
    return os.environ.get("OSTIARI_ENV", "").strip().lower() in ("production", "prod")


def data_dir() -> Path:
    """Directory for writable runtime state (SQLite db, state.json).

    Honors ``OSTIARI_DATA_DIR``, else falls back to ``<repo>/control-plane/data``
    for a dev checkout.

    Exists because the two callers previously derived this from ``__file__``
    independently, with a different number of ``.parent`` hops, and so disagreed:
    the database landed in ``control-plane/data`` while state.json landed in
    ``control-plane/backend/data``. In the container (which runs from ``/app``)
    that split put state.json in ``/app/data`` — a root-owned directory the
    non-root runtime user cannot create — while only the database was redirected
    onto the mounted volume by ``DATABASE_URL``. ``save_state`` then raised
    PermissionError during shutdown, so every restart discarded the persisted
    stores; and the read-only root filesystem was unreachable on top of that.

    Deliberately no mkdir here: a resolver that has a filesystem side effect at
    import time is exactly the problem being fixed. Callers create it when they
    are about to write.
    """
    override = os.environ.get("OSTIARI_DATA_DIR", "").strip()
    if override:
        return Path(override)
    # …/control-plane/backend/control_plane/env.py → …/control-plane/data
    return Path(__file__).resolve().parent.parent.parent / "data"


def default_sqlite_url() -> str:
    """SQLite URL under :func:`data_dir`, creating the directory.

    Used only when ``DATABASE_URL`` is unset, i.e. dev checkouts — every image and
    manifest in ``deploy/`` sets it. Deliberately a function rather than a module
    constant: as a constant the mkdir ran at *import* time, so merely importing the
    app wrote to disk.

    Both ``control_plane.database`` and ``alembic/env.py`` call this. They used to
    each derive the path from their own ``__file__`` with a different number of
    ``.parent`` hops, which is a silent-drift hazard: `alembic upgrade head` with no
    DATABASE_URL would migrate one file while the app opened another, so the
    migration reported success and the app still failed on the missing column.
    """
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{d / 'control_plane.db'}"


# The known-insecure default JWT secret; refused in production.
DEFAULT_DEV_JWT_SECRET = "ostiari-dev-secret-change-in-prod"
