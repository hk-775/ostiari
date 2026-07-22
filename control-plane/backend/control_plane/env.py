"""Deployment-environment helpers.

A single, explicit production signal so security-sensitive defaults can be
permissive in dev/demo but fail-closed in production. Set OSTIARI_ENV=production
(or prod) in real deployments.
"""

from __future__ import annotations

import os


def is_production() -> bool:
    """True when OSTIARI_ENV indicates a production deployment."""
    return os.environ.get("OSTIARI_ENV", "").strip().lower() in ("production", "prod")


# The known-insecure default JWT secret; refused in production.
DEFAULT_DEV_JWT_SECRET = "ostiari-dev-secret-change-in-prod"
