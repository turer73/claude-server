"""Prometheus metrics endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core.prometheus_exporter import PrometheusExporter

router = APIRouter(tags=["metrics"])

_exporter = PrometheusExporter()


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    return _exporter.export()
