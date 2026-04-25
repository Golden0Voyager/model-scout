"""Orchestrates model discovery and health checks."""

import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from core.config import get_static_models, get_provider_config, ProviderConfig, ModelConfig
from core.database import (
    init_db,
    upsert_health,
    get_all_health,
    log_scan_start,
    log_scan_finish,
    get_last_scan_time,
)
from services.health_checker import HealthChecker, ProbeResult


class SyncService:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.is_scanning = False
        self.last_scan_time: Optional[str] = None
        self._checker: Optional[HealthChecker] = None
        self._discovered_models: List[ModelConfig] = []
        self._discovered_at: Optional[datetime] = None

    def _get_all_models(self) -> List[ModelConfig]:
        """Return static + discovered models."""
        static = get_static_models()
        # Merge: static models take precedence over discovered ones
        static_keys = {(m.provider, m.id) for m in static}
        merged = list(static)
        for dm in self._discovered_models:
            if (dm.provider, dm.id) not in static_keys:
                merged.append(dm)
        return merged

    async def _refresh_discovered_models(self) -> None:
        """Discover models from dynamic providers."""
        if not self._checker:
            return
        from core.config import PROVIDERS
        discovered: List[ModelConfig] = []
        static_keys = {(m.provider, m.id) for m in get_static_models()}

        for provider_key, provider in PROVIDERS.items():
            if provider.discovery != "dynamic" or not provider.models_endpoint or not provider.auto_discover:
                continue

            # OpenRouter: detailed discovery, keep free models only
            if provider_key == "openrouter":
                models_info, error = await self._checker.discover_models_detailed(provider_key)
                if not models_info:
                    if error:
                        print(f"⚠️ Discovery failed for {provider.name}: {error}")
                    continue
                free_models = [m for m in models_info if m.get("is_free")]
                for info in free_models:
                    mid = info["id"]
                    if (provider_key, mid) in static_keys:
                        continue
                    discovered.append(ModelConfig(
                        id=mid,
                        name=info.get("name", mid),
                        provider=provider_key,
                        context_length=0,
                        description=f"Auto-discovered from {provider.name}",
                        description_cn=f"从 {provider.name} 自动发现",
                        capabilities=["chat"],
                        pricing_input_per_1m=info.get("pricing_input_per_1m"),
                        pricing_output_per_1m=info.get("pricing_output_per_1m"),
                        pricing_currency="USD",
                        is_free=True,
                        probe_mode="chat",
                    ))
                print(f"🔎 {provider.name}: discovered {len(free_models)} free models")
                continue

            # Other providers: standard ID-only discovery
            model_ids, error = await self._checker.discover_models(provider_key)
            if not model_ids:
                if error:
                    print(f"⚠️ Discovery failed for {provider.name}: {error}")
                continue
            for mid in model_ids:
                if (provider_key, mid) in static_keys:
                    continue
                # Infer reasonable defaults from model ID
                inferred_ctx = 128000
                if any(k in mid.lower() for k in ["32k", "-32b"]):
                    inferred_ctx = 32000
                elif any(k in mid.lower() for k in ["256k", "-256b"]):
                    inferred_ctx = 256000
                elif any(k in mid.lower() for k in ["1m", "1000000", "1000k"]):
                    inferred_ctx = 1000000

                discovered.append(ModelConfig(
                    id=mid,
                    name=mid,
                    provider=provider_key,
                    context_length=inferred_ctx,
                    description=f"Auto-discovered from {provider.name}",
                    description_cn=f"从 {provider.name} 自动发现",
                    capabilities=["chat"],
                    probe_mode="chat",
                ))

        self._discovered_models = discovered
        self._discovered_at = datetime.now(timezone.utc)
        if discovered:
            print(f"🔎 Discovered {len(discovered)} new models from dynamic providers")

    async def initialize(self) -> None:
        await init_db()
        self._checker = HealthChecker(proxy=self.proxy)
        await self._checker.__aenter__()

    async def shutdown(self) -> None:
        if self._checker:
            await self._checker.__aexit__(None, None, None)

    async def run_sync(self) -> Dict[str, Any]:
        """Full sync: probe all configured models."""
        if self.is_scanning:
            return {"status": "already_scanning"}

        self.is_scanning = True
        scan_id = await log_scan_start()
        start_time = datetime.now(timezone.utc)

        try:
            # Discover new models from dynamic providers first
            await self._refresh_discovered_models()

            models = self._get_all_models()
            probes = [{"model_id": m.id, "provider": m.provider} for m in models if m.probe_mode != "none"]
            skipped = [m for m in models if m.probe_mode == "none"]

            print(f"🔍 Starting health check for {len(probes)} models ({len(skipped)} skipped)...")
            results: List[ProbeResult] = await self._checker.probe_batch(probes, concurrency=6)

            online_count = 0
            for r in results:
                if r.status == "online":
                    online_count += 1
                await upsert_health({
                    "model_id": r.model_id,
                    "provider": r.provider,
                    "status": r.status,
                    "latency_ms": r.latency_ms,
                    "error_message": r.error_message,
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                })

            # Mark skipped models as unknown with no error
            for m in skipped:
                await upsert_health({
                    "model_id": m.id,
                    "provider": m.provider,
                    "status": "unknown",
                    "latency_ms": None,
                    "error_message": "Probe disabled for this provider",
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                })

            await log_scan_finish(scan_id, len(probes), online_count)
            self.last_scan_time = datetime.now(timezone.utc).isoformat()

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            print(f"✅ Sync complete in {duration:.1f}s: {online_count}/{len(probes)} online ({len(skipped)} skipped)")

            return {
                "status": "success",
                "checked": len(probes),
                "online": online_count,
                "skipped": len(skipped),
                "duration_sec": duration,
            }

        except Exception as e:
            await log_scan_finish(scan_id, 0, 0, error=str(e)[:200])
            print(f"❌ Sync failed: {e}")
            return {"status": "error", "message": str(e)}

        finally:
            self.is_scanning = False

    async def probe_single_model(self, model_id: str, provider_key: str) -> ProbeResult:
        """Probe a single model and persist result."""
        if not self._checker:
            return ProbeResult(
                model_id=model_id, provider=provider_key,
                status="error", error_message="Health checker not initialized"
            )
        result = await self._checker.probe(model_id, provider_key)
        await upsert_health({
            "model_id": result.model_id,
            "provider": result.provider,
            "status": result.status,
            "latency_ms": result.latency_ms,
            "error_message": result.error_message,
            "last_checked": datetime.now(timezone.utc).isoformat(),
        })
        return result

    async def probe_provider(self, provider_key: str) -> Dict[str, Any]:
        """Probe all models for a single provider."""
        if not self._checker:
            return {"status": "error", "message": "Health checker not initialized"}

        provider = get_provider_config(provider_key)
        if not provider:
            return {"status": "error", "message": f"Unknown provider: {provider_key}"}

        models = [m for m in self._get_all_models() if m.provider == provider_key and m.probe_mode != "none"]
        if not models:
            return {"status": "error", "message": f"No probe-able models found for {provider_key}"}

        probes = [{"model_id": m.id, "provider": m.provider} for m in models]
        results = await self._checker.probe_batch(probes, concurrency=6)

        online_count = 0
        for r in results:
            if r.status == "online":
                online_count += 1
            await upsert_health({
                "model_id": r.model_id,
                "provider": r.provider,
                "status": r.status,
                "latency_ms": r.latency_ms,
                "error_message": r.error_message,
                "last_checked": datetime.now(timezone.utc).isoformat(),
            })

        return {
            "status": "success",
            "provider": provider_key,
            "checked": len(probes),
            "online": online_count,
        }

    async def get_dashboard_data(self) -> Dict[str, Any]:
        """Combine static model catalog with latest health data."""
        # Refresh discovered models if we have none yet
        if not self._discovered_models and self._checker:
            await self._refresh_discovered_models()

        models = self._get_all_models()
        health_rows = await get_all_health()
        health_map: Dict[str, Dict[str, Any]] = {
            f"{r['provider']}::{r['model_id']}": r for r in health_rows
        }

        provider_stats: Dict[str, Dict[str, Any]] = {}
        total_online = 0
        latencies: List[int] = []

        enriched_models = []
        for m in models:
            key = f"{m.provider}::{m.id}"
            h = health_map.get(key, {})

            status = h.get("status", "unknown")
            latency = h.get("latency_ms")
            if status == "online":
                total_online += 1
                if latency:
                    latencies.append(latency)

            provider = get_provider_config(m.provider)
            provider_name = provider.name if provider else m.provider

            if m.provider not in provider_stats:
                provider_stats[m.provider] = {
                    "key": m.provider,
                    "name": provider_name,
                    "model_count": 0,
                    "online_count": 0,
                    "latencies": [],
                }
            provider_stats[m.provider]["model_count"] += 1
            if status == "online":
                provider_stats[m.provider]["online_count"] += 1
                if latency:
                    provider_stats[m.provider]["latencies"].append(latency)

            enriched_models.append({
                "id": m.id,
                "name": m.name,
                "provider": m.provider,
                "provider_name": provider_name,
                "context_length": m.context_length,
                "max_output_tokens": m.max_output_tokens,
                "description": m.description,
                "description_cn": m.description_cn,
                "capabilities": m.capabilities,
                "pricing_input_per_1m": m.pricing_input_per_1m,
                "pricing_output_per_1m": m.pricing_output_per_1m,
                "pricing_currency": m.pricing_currency,
                "pricing_note": m.pricing_note,
                "is_free": m.is_free,
                "health": {
                    "model_id": m.id,
                    "provider": m.provider,
                    "status": status,
                    "latency_ms": latency,
                    "error_message": h.get("error_message"),
                    "last_checked": h.get("last_checked"),
                },
            })

        providers = []
        for p in provider_stats.values():
            avg_lat = int(sum(p["latencies"]) / len(p["latencies"])) if p["latencies"] else None
            providers.append({
                "key": p["key"],
                "name": p["name"],
                "model_count": p["model_count"],
                "online_count": p["online_count"],
                "avg_latency_ms": avg_lat,
            })

        providers.sort(key=lambda x: x["name"])

        if not self.last_scan_time:
            self.last_scan_time = await get_last_scan_time()

        return {
            "models": enriched_models,
            "providers": providers,
            "total_models": len(models),
            "online_models": total_online,
            "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
            "last_scan_time": self.last_scan_time,
            "is_scanning": self.is_scanning,
        }
