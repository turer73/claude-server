"""Prometheus metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.api.memory import verify_key
from app.core.prometheus_exporter import PrometheusExporter

router = APIRouter(tags=["metrics"])

_exporter = PrometheusExporter()


@router.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(verify_key)])
async def prometheus_metrics():
    return _exporter.export()
