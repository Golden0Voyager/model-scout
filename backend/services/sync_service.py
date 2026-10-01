"""Orchestrates model discovery and health checks."""

import asyncio
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from core.config import ModelConfig, get_provider_config, get_static_models, provider_enabled
from core.database import (
    get_all_health,
    get_last_scan_time,
    init_db,
    log_scan_finish,
    log_scan_start,
    upsert_health,
)
from services.health_checker import HealthChecker, ProbeResult

# Guards against cost amplification: each full scan spends real inference requests.
MIN_SCAN_INTERVAL_SECONDS = 20.0


class SyncService:
    def __init__(self, proxy: str | None = None):
        self.proxy = proxy
        self.is_scanning = False
        self.last_scan_time: str | None = None
        self._checker: HealthChecker | None = None
        self._discovered_models: list[ModelConfig] = []
        self._discovered_at: datetime | None = None
        self._scan_lock = asyncio.Lock()
        self._last_scan_started = 0.0
        self._background_scan: asyncio.Task[None] | None = None

    async def acquire_scan(self) -> str | None:
        """Atomically claim the single scan slot. Returns None on success, else a verdict."""
        async with self._scan_lock:
            if self.is_scanning:
                return "already_scanning"
            remaining = MIN_SCAN_INTERVAL_SECONDS - (time.monotonic() - self._last_scan_started)
            if remaining > 0:
                return f"cooldown:{int(remaining) + 1}s"
            self.is_scanning = True
            self._last_scan_started = time.monotonic()
            return None

    def release_scan(self) -> None:
        self.is_scanning = False

    def _get_all_models(self) -> list[ModelConfig]:
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
        discovered: list[ModelConfig] = []
        static_keys = {(m.provider, m.id) for m in get_static_models()}

        for provider_key, provider in PROVIDERS.items():
            if not provider.enabled:
                continue
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

            # Providers whose /models carries per-model metadata (Moonshot, SenseNova).
            if provider.rich_discovery:
                models_info, error = await self._checker.discover_models_detailed(provider_key)
                if not models_info:
                    if error:
                        print(f"⚠️ Discovery failed for {provider.name}: {error}")
                    continue
                for info in models_info:
                    mid = info["id"]
                    if (provider_key, mid) in static_keys:
                        continue
                    discovered.append(ModelConfig(
                        id=mid,
                        name=info.get("name", mid),
                        provider=provider_key,
                        context_length=info.get("context_length") or 128000,
                        max_output_tokens=info.get("max_output_tokens"),
                        description=info.get("description") or f"Auto-discovered from {provider.name}",
                        description_cn=f"从 {provider.name} 自动发现",
                        capabilities=info.get("capabilities", ["chat"]),
                        pricing_input_per_1m=info.get("pricing_input_per_1m"),
                        pricing_output_per_1m=info.get("pricing_output_per_1m"),
                        pricing_currency="USD",
                        is_free=bool(info.get("is_free")),
                        probe_mode="chat",
                    ))
                print(f"🔎 {provider.name}: discovered {len(models_info)} models with metadata")
                continue

            # Other providers: standard ID-only discovery
            model_ids, error = await self._checker.discover_models(provider_key)
            if not model_ids:
                if error:
                    print(f"⚠️ Discovery failed for {provider.name}: {error}")
                continue
            for mid in model_ids:
                # Gemini API returns IDs with "models/" prefix — strip it
                if mid.startswith("models/"):
                    mid = mid[len("models/"):]
                if (provider_key, mid) in static_keys:
                    continue
                # Skip non-chat models (embedding, image gen, video, audio, TTS, etc.)
                _skip = ["embedding", "imagen", "veo", "lyria", "deep-research",
                         "-tts", "-audio", "-live", "-image-preview", "robotics",
                         "computer-use", "aqa", "antigravity", "nano-banana",
                         "-customtools"]
                if any(k in mid.lower() for k in _skip):
                    continue
                # Infer reasonable defaults from model ID
                inferred_ctx = 128000
                if any(k in mid.lower() for k in ["32k", "-32b"]):
                    inferred_ctx = 32000
                elif any(k in mid.lower() for k in ["256k", "-256b"]):
                    inferred_ctx = 256000
                elif any(k in mid.lower() for k in ["1m", "1000000", "1000k"]):
                    inferred_ctx = 1000000
                # Gemini chat models default to 1M context (except Gemma)
                if provider_key == "gemini" and mid.startswith("gemini-"):
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
        self._discovered_at = datetime.now(UTC)
        if discovered:
            print(f"🔎 Discovered {len(discovered)} new models from dynamic providers")

    async def initialize(self) -> None:
        await init_db()
        self._checker = HealthChecker(proxy=self.proxy)
        await self._checker.__aenter__()

    async def shutdown(self) -> None:
        if self._background_scan and not self._background_scan.done():
            self._background_scan.cancel()
            await asyncio.gather(self._background_scan, return_exceptions=True)
        if self._checker:
            await self._checker.__aexit__(None, None, None)

    async def run_sync(self) -> dict[str, Any]:
        """Full sync: probe all configured models. No-ops if the scan slot is taken."""
        verdict = await self.acquire_scan()
        if verdict is not None:
            return {"status": verdict}
        try:
            return await self._execute_scan()
        finally:
            self.release_scan()

    def _spawn_scan(self, label: str, work: Coroutine[Any, Any, Any]) -> None:
        """Run an already-claimed scan in the background, releasing the slot when done."""

        async def _runner() -> None:
            try:
                print(f"✅ {label}: {await work}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"❌ {label} failed: {e}")
            finally:
                self.release_scan()

        self._background_scan = asyncio.create_task(_runner())

    async def start_scan(self) -> str | None:
        """Claim the scan slot and run a full scan in the background; returns a verdict or None."""
        verdict = await self.acquire_scan()
        if verdict is not None:
            return verdict
        self._spawn_scan("Scan", self._execute_scan())
        return None

    async def start_provider_scan(self, provider_key: str) -> str | None:
        """Probe one provider in the background; returns a verdict or None when started."""
        checker = self._checker
        if checker is None:
            return "service_not_initialized"

        models = [m for m in self._get_all_models() if m.provider == provider_key and m.probe_mode != "none"]
        if not models:
            return f"no_probeable_models:{provider_key}"

        verdict = await self.acquire_scan()
        if verdict is not None:
            return verdict
        self._spawn_scan(
            f"Provider scan {provider_key}",
            self._probe_provider_models(provider_key, models, checker),
        )
        return None

    async def _execute_scan(self) -> dict[str, Any]:
        checker = self._checker
        if checker is None:
            return {"status": "error", "message": "Health checker not initialized"}

        scan_id = await log_scan_start()
        start_time = datetime.now(UTC)

        try:
            # Discover new models from dynamic providers first
            await self._refresh_discovered_models()

            models = self._get_all_models()
            probeable: list[ModelConfig] = []
            skipped: list[ModelConfig] = []
            for m in models:
                if m.probe_mode == "none" or not provider_enabled(m.provider):
                    skipped.append(m)
                else:
                    probeable.append(m)
            probes = [{"model_id": m.id, "provider": m.provider} for m in probeable]

            print(f"🔍 Starting health check for {len(probes)} models ({len(skipped)} skipped)...")
            results: list[ProbeResult] = await checker.probe_batch(probes, concurrency=6)

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
                    "last_checked": datetime.now(UTC).isoformat(),
                })

            # Mark skipped models as unknown with no error
            for m in skipped:
                await upsert_health({
                    "model_id": m.id,
                    "provider": m.provider,
                    "status": "unknown",
                    "latency_ms": None,
                    "error_message": "Probe disabled for this provider",
                    "last_checked": datetime.now(UTC).isoformat(),
                })

            await log_scan_finish(scan_id, len(probes), online_count)
            self.last_scan_time = datetime.now(UTC).isoformat()

            duration = (datetime.now(UTC) - start_time).total_seconds()
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
            "last_checked": datetime.now(UTC).isoformat(),
        })
        return result

    async def _probe_provider_models(
        self, provider_key: str, models: list[ModelConfig], checker: HealthChecker
    ) -> dict[str, Any]:
        probes = [{"model_id": m.id, "provider": m.provider} for m in models]
        results = await checker.probe_batch(probes, concurrency=6)

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
                "last_checked": datetime.now(UTC).isoformat(),
            })

        return {
            "status": "success",
            "provider": provider_key,
            "checked": len(probes),
            "online": online_count,
        }

    async def get_dashboard_data(self) -> dict[str, Any]:
        """Combine the model catalog with the latest persisted health data.

        Read-only by design: discovery and probing are driven by the scheduler and
        explicit scan requests, never by a dashboard poll.
        """
        models = self._get_all_models()
        health_rows = await get_all_health()
        health_map: dict[str, dict[str, Any]] = {
            f"{r['provider']}::{r['model_id']}": r for r in health_rows
        }

        provider_stats: dict[str, dict[str, Any]] = {}
        total_online = 0
        latencies: list[int] = []

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
