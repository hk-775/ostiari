"""Report API router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response, StreamingResponse

from ostiari.dashboard.dependencies import get_report_generator
from ostiari.report import ReportGenerator

router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("")
async def generate_report(
    period: int = 7,
    format: str = "json",
    generator: ReportGenerator = Depends(get_report_generator),
) -> Response:
    if format == "csv":
        return StreamingResponse(
            generator.generate_csv_rows(period),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=ostiari-report-{period}d.csv"
            },
        )

    data = generator.generate(period_days=period, format="json")
    return Response(content=data, media_type="application/json")
