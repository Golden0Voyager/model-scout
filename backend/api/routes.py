"""FastAPI routes for ModelScout."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from core.access import PROBE_MAX_CALLS, PROBE_WINDOW_SECONDS, probe_limiter, require_trusted_origin
from core.config import FALLBACK_CNY_PER_USD, get_provider_config
from core.models import (
    DashboardResponse,
    ProviderSettingsResponse,
    ProviderToggleRequest,
    ScanTriggerResponse,
)
from core.provider_state import is_enabled
from services.fx import FxRate
from services.sync_service import SyncService

router = APIRouter()

# Wired by app.py during the lifespan startup.
sync_service: SyncService | None = None
fx_rate: FxRate | None = None

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
        elif kind == "provider_disabled":
            message = f"Provider '{detail}' is switched off in settings"
        else:
            message = verdict
    return ScanTriggerResponse(status="rejected", message=message)


async def _require_provider(provider_key: str) -> None:
    if get_provider_config(provider_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_key}")
    if not await is_enabled(provider_key):
        raise HTTPException(
            status_code=409,
            detail=f"Provider '{provider_key}' is switched off in settings",
        )


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
    data = await _service().get_dashboard_data()
    # Cached server-side; the read path itself never reaches out for it.
    data["cny_per_usd"] = fx_rate.cny_per_usd if fx_rate is not None else FALLBACK_CNY_PER_USD
    return data


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
    await _require_provider(provider_key)
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
    await _require_provider(provider_key)
    _enforce_probe_limit()
    result = await _service().probe_single_model(model_id, provider_key)
    return {
        "status": result.status,
        "model_id": result.model_id,
        "provider": result.provider,
        "latency_ms": result.latency_ms,
        "error_message": result.error_message,
    }


@router.get("/providers", response_model=ProviderSettingsResponse)
async def get_providers() -> dict[str, Any]:
    """List every provider, switched-off ones included — that is the point of the screen."""
    return {"providers": await _service().get_provider_settings()}


@router.put(
    "/providers/{provider_key}",
    response_model=ProviderSettingsResponse,
    dependencies=[Depends(require_trusted_origin)],
)
async def set_provider(
    provider_key: str,
    body: ProviderToggleRequest,
) -> dict[str, Any]:
    """Switch a provider on or off. Off hides it from the dashboard and from scans."""
    if get_provider_config(provider_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_key}")
    service = _service()
    await service.set_provider_enabled(provider_key, body.enabled)
    # The whole list comes back so the switch settles on its stored value rather than
    # on whatever the browser last rendered.
    return {"providers": await service.get_provider_settings()}
