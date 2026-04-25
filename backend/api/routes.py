"""FastAPI routes for ModelScout."""

from fastapi import APIRouter
from typing import Dict, Any

from services.sync_service import SyncService
from core.models import DashboardResponse, ScanTriggerResponse

router = APIRouter()

# Populated by app.py on startup
sync_service: SyncService = None  # type: ignore


@router.get("/models", response_model=DashboardResponse)
async def get_models() -> Dict[str, Any]:
    """Get all models with their current health status."""
    return await sync_service.get_dashboard_data()


@router.get("/models/{model_id}")
async def get_model_detail(model_id: str) -> Dict[str, Any]:
    """Get detail for a single model by its ID."""
    data = await sync_service.get_dashboard_data()
    for m in data.get("models", []):
        if m["id"] == model_id:
            return m
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")


@router.post("/scan", response_model=ScanTriggerResponse)
async def trigger_scan() -> Dict[str, str]:
    """Trigger a manual health check scan."""
    if sync_service.is_scanning:
        return {"status": "already_scanning", "message": "A scan is already in progress"}

    import asyncio
    asyncio.create_task(sync_service.run_sync())
    return {"status": "scan_started", "message": "Background scan initiated"}


@router.post("/scan/{provider_key}")
async def trigger_provider_scan(provider_key: str) -> Dict[str, Any]:
    """Trigger health check for all models of a single provider."""
    if sync_service.is_scanning:
        return {"status": "already_scanning", "message": "A scan is already in progress"}

    import asyncio
    # Run in background so we don't block
    async def _do():
        result = await sync_service.probe_provider(provider_key)
        print(f"🔍 Provider scan {provider_key}: {result}")
        return result

    asyncio.create_task(_do())
    return {"status": "scan_started", "message": f"Background scan initiated for {provider_key}"}


@router.post("/scan/{provider_key}/{model_id}")
async def trigger_model_scan(provider_key: str, model_id: str) -> Dict[str, Any]:
    """Trigger health check for a single model."""
    result = await sync_service.probe_single_model(model_id, provider_key)
    return {
        "status": result.status,
        "model_id": result.model_id,
        "provider": result.provider,
        "latency_ms": result.latency_ms,
        "error_message": result.error_message,
    }
