"""Config API router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ostiari.config import ConfigLoader

router = APIRouter(prefix="/api/config", tags=["config"])

REDACTED_KEYS = {"token", "password", "secret", "api_key", "credentials"}


@router.get("")
async def get_config() -> dict[str, Any]:
    try:
        config = ConfigLoader.load()
        data = config.model_dump(mode="json")
        return _redact(data)
    except Exception as e:
        return {"error": str(e)}


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 10:
        return obj
    if isinstance(obj, dict):
        return {
            k: "***REDACTED***"
            if any(s in k.lower() for s in REDACTED_KEYS)
            else _redact(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, depth + 1) for item in obj]
    return obj
