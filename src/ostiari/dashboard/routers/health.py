"""Health API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse

from ostiari.dashboard.dependencies import get_health_checker
from ostiari.health import HealthChecker

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health_check(
    checker: HealthChecker = Depends(get_health_checker),
) -> JSONResponse:
    result = checker.run()
    status_code = 200 if result["status"] == "ok" else 503
    return JSONResponse(content=result, status_code=status_code)
