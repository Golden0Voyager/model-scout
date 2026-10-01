"""FastAPI routes for ModelScout."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from core.access import PROBE_MAX_CALLS, PROBE_WINDOW_SECONDS, probe_limiter, require_trusted_origin
from core.config import get_provider_config
from core.models import DashboardResponse, ScanTriggerResponse
from services.sync_service import SyncService

router = APIRouter()

# Wired by app.py during the lifespan startup.
sync_service: SyncService | None = None

_VERDICT_MESSAGES = {
    "already_scanning": "A scan is already in progress",
    "service_not_initialized": "Service is still starting up",
}


def _service() -> SyncService:
    if sync_service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return sync_service


def _rejected(verdict: str) -> ScanTriggerResponse:
    message = _VERDICT_MESSAGES.get(verdict)
    if message is None:
        kind, _, detail = verdict.partition(":")
        if kind == "cooldown":
            message = f"Scans are rate limited, retry in {detail}"
        elif kind == "no_probeable_models":
            message = f"No probe-able models found for {detail}"
        else:
            message = verdict
    return ScanTriggerResponse(status="rejected", message=message)


def _require_provider(provider_key: str) -> None:
    if get_provider_config(provider_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_key}")


def _enforce_probe_limit() -> None:
    """Per-model probes each spend one real request and bypass the scan slot."""
    retry_after = probe_limiter.acquire()
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Probe rate limit reached ({PROBE_MAX_CALLS} per {int(PROBE_WINDOW_SECONDS)}s)",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


@router.get("/models", response_model=DashboardResponse)
async def get_models() -> dict[str, Any]:
    """Get all models with their current health status."""
    return await _service().get_dashboard_data()


@router.get("/models/{model_id:path}")
async def get_model_detail(model_id: str) -> dict[str, Any]:
    """Get detail for a single model by its ID."""
    data = await _service().get_dashboard_data()
    for m in data.get("models", []):
        if m["id"] == model_id:
            return m
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")


@router.post(
    "/scan",
    response_model=ScanTriggerResponse,
    dependencies=[Depends(require_trusted_origin)],
)
async def trigger_scan() -> ScanTriggerResponse:
    """Trigger a manual health check scan."""
    verdict = await _service().start_scan()
    if verdict is not None:
        return _rejected(verdict)
    return ScanTriggerResponse(status="scan_started", message="Background scan initiated")


@router.post(
    "/scan/{provider_key}",
    response_model=ScanTriggerResponse,
    dependencies=[Depends(require_trusted_origin)],
)
async def trigger_provider_scan(provider_key: str) -> ScanTriggerResponse:
    """Trigger health check for all models of a single provider."""
    _require_provider(provider_key)
    verdict = await _service().start_provider_scan(provider_key)
    if verdict is not None:
        return _rejected(verdict)
    return ScanTriggerResponse(
        status="scan_started", message=f"Background scan initiated for {provider_key}"
    )


@router.post(
    "/scan/{provider_key}/{model_id:path}",
    dependencies=[Depends(require_trusted_origin)],
)
async def trigger_model_scan(provider_key: str, model_id: str) -> dict[str, Any]:
    """Trigger health check for a single model. The ID may contain slashes."""
    _require_provider(provider_key)
    _enforce_probe_limit()
    result = await _service().probe_single_model(model_id, provider_key)
    return {
        "status": result.status,
        "model_id": result.model_id,
        "provider": result.provider,
        "latency_ms": result.latency_ms,
        "error_message": result.error_message,
    }
