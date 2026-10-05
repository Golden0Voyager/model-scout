"""Orchestrates model discovery and health checks."""

import asyncio
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from core.config import PROVIDERS, ModelConfig, get_provider_config, get_static_models
from core.database import (
    clear_retirement,
    get_all_health,
    get_last_scan_time,
    get_retirements,
    init_db,
    log_scan_finish,
    log_scan_start,
    retire_model,
    set_provider_pref,
    upsert_health,
)
from core.provider_state import effective_enabled
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
        # (provider_key, model_id) the upstream no longer serves. Loaded at startup so
        # a restart does not resurrect dead models for one scan.
        self._retired: set[tuple[str, str]] = set()
        # provider_key -> the ids its /models listed during the current scan, before any
        # filtering. Retirement reads this: it is the live catalogue the scan actually
        # saw, whether discovery or probing pulled it.
        self._listed: dict[str, set[str]] = {}

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

    async def _refresh_discovered_models(self, enabled: dict[str, bool]) -> None:
        """Discover models from dynamic providers that are currently switched on."""
        if not self._checker:
            return
        self._listed = {}
        discovered: list[ModelConfig] = []
        static_keys = {(m.provider, m.id) for m in get_static_models()}

        for provider_key, provider in PROVIDERS.items():
            if not enabled.get(provider_key, False):
                continue
            if provider.discovery != "dynamic" or not provider.models_endpoint or not provider.auto_discover:
                continue

            # OpenRouter: detailed discovery, keep free models only
            if provider_key == "openrouter":
                models_info, error = await self._checker.discover_models_detailed(provider_key)
                if models_info is None:
                    if error:
                        print(f"⚠️ Discovery failed for {provider.name}: {error}")
                    continue
                self._listed[provider_key] = {
                    str(info["id"]) for info in models_info if info.get("id")
                }
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
                if models_info is None:
                    if error:
                        print(f"⚠️ Discovery failed for {provider.name}: {error}")
                    continue
                self._listed[provider_key] = {
                    str(info["id"]) for info in models_info if info.get("id")
                }
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
                        pricing_currency=info.get("pricing_currency") or "USD",
                        pricing_note=info.get("pricing_note", ""),
                        is_free=bool(info.get("is_free")),
                        probe_mode="chat",
                    ))
                print(f"🔎 {provider.name}: discovered {len(models_info)} models with metadata")
                continue

            # Other providers: standard ID-only discovery
            model_ids, error = await self._checker.discover_models(provider_key)
            if model_ids is None:
                if error:
                    print(f"⚠️ Discovery failed for {provider.name}: {error}")
                continue
            self._listed[provider_key] = {
                # Gemini prefixes its ids with "models/" while every caller uses the bare
                # id. Recorded unnormalised, a live model would look unlisted.
                mid.removeprefix("models/") for mid in model_ids
            }
            for mid in model_ids:
                # Gemini API returns IDs with "models/" prefix — strip it
                if mid.startswith("models/"):
                    mid = mid[len("models/"):]
                if (provider_key, mid) in static_keys:
                    continue
                # Skip non-chat models (embedding, image gen, video, audio, TTS, etc.)
                _skip = ["embedding", "imagen", "veo", "lyria", "deep-research",
                         "-tts", "-asr", "-audio", "-live", "-image-preview", "robotics",
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

    def _visible(self, models: list[ModelConfig]) -> list[ModelConfig]:
        """Drop models the upstream has stopped serving.

        A pinned row is not a claim that the model still exists, so it yields to what
        the live catalogue and the provider's own answer say about it.
        """
        return [m for m in models if (m.provider, m.id) not in self._retired]

    async def initialize(self) -> None:
        await init_db()
        self._retired = set(await get_retirements())
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
        if not (await effective_enabled()).get(provider_key, False):
            return f"provider_disabled:{provider_key}"

        models = [
            m
            for m in self._visible(self._get_all_models())
            if m.provider == provider_key and m.probe_mode != "none"
        ]
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

        # One snapshot per scan: every model of a provider should see the same
        # catalogue, fetched once.
        checker.reset_model_cache()
        scan_id = await log_scan_start()
        start_time = datetime.now(UTC)

        try:
            enabled = await effective_enabled()

            # Discover new models from dynamic providers first
            await self._refresh_discovered_models(enabled)

            models = self._visible(self._get_all_models())
            probeable: list[ModelConfig] = []
            skipped: list[ModelConfig] = []
            for m in models:
                if m.probe_mode == "none" or not enabled.get(m.provider, False):
                    skipped.append(m)
                else:
                    probeable.append(m)
            probes = [{"model_id": m.id, "provider": m.provider} for m in probeable]

            print(f"🔍 Starting health check for {len(probes)} models ({len(skipped)} skipped)...")
            results: list[ProbeResult] = await checker.probe_batch(probes, concurrency=6)
            retired_now = await self._apply_retirements(checker, results)
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
            if retired_now:
                print(f"🗑️  Retired {retired_now} model(s) the upstream no longer serves")

            return {
                "status": "success",
                "checked": len(probes),
                "online": online_count,
                "skipped": len(skipped),
                "retired": retired_now,
                "duration_sec": duration,
            }

        except Exception as e:
            await log_scan_finish(scan_id, 0, 0, error=str(e)[:200])
            print(f"❌ Sync failed: {e}")
            return {"status": "error", "message": str(e)}

    def _catalogue_seen(self, checker: HealthChecker, provider_key: str) -> set[str] | None:
        """The ids this provider listed during the current scan, or None if unknown.

        Discovery records the list even for providers whose per-model metadata path
        never touches the probe cache, so both routes count as evidence.
        """
        listed = self._listed.get(provider_key)
        return listed if listed is not None else checker.listed_ids(provider_key)

    async def _apply_retirements(
        self,
        checker: HealthChecker,
        results: list[ProbeResult],
        providers: set[str] | None = None,
    ) -> int:
        """Retire models the upstream proved gone, and revive the ones that came back.

        Two signals must agree inside one scan before a row disappears: the provider's
        own chat endpoint has to reject the model by identity, and its freshly fetched
        catalogue has to omit it. Either signal alone is unreliable — DeepSeek returns a
        two-entry /models while still serving deepseek-chat, and a rate limit or an empty
        balance says nothing about whether the model exists.

        `providers` limits the sweep to the providers a refresh actually touched, so
        refreshing one does not go reading other providers' catalogues.

        Returns how many rows were newly retired.
        """
        retired_now = 0
        for r in results:
            if r.status != "offline" or (r.provider, r.model_id) in self._retired:
                continue
            listed = self._catalogue_seen(checker, r.provider)
            if listed is None or r.model_id in listed:
                continue
            await retire_model(r.provider, r.model_id)
            self._retired.add((r.provider, r.model_id))
            retired_now += 1

        pending = [
            (provider_key, model_id)
            for provider_key, model_id in sorted(self._retired)
            if providers is None or provider_key in providers
        ]
        for provider_key, model_id in pending:
            listed = self._catalogue_seen(checker, provider_key)
            if listed is None:
                # Retired models are never probed, so nothing else would have pulled
                # this provider's catalogue. Without this one free GET a provider
                # whose every model is retired could never revive itself. The lookup
                # is cached per provider, so repeated retired rows cost one request.
                provider = get_provider_config(provider_key)
                if provider is None or provider.discovery != "dynamic" or not provider.models_endpoint:
                    continue
                await checker.discover_models(provider_key)
                listed = self._catalogue_seen(checker, provider_key)
            if listed is not None and model_id in listed:
                await clear_retirement(provider_key, model_id)
                self._retired.discard((provider_key, model_id))
                print(f"♻️  {provider_key}/{model_id} is back on the catalogue")

        return retired_now

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
        # A refresh should actually re-read the provider, not serve a stale snapshot.
        checker.reset_model_cache()
        probes = [{"model_id": m.id, "provider": m.provider} for m in models]
        results = await checker.probe_batch(probes, concurrency=6)
        retired_now = await self._apply_retirements(checker, results, {provider_key})

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
            "retired": retired_now,
        }

    async def set_provider_enabled(self, provider_key: str, enabled: bool) -> None:
        """Record the switch and refresh the panel when a provider comes back on."""
        await set_provider_pref(provider_key, enabled)
        if enabled:
            # A provider scan is not enough: providers whose catalogue is entirely
            # discovered have no known models yet, so only a full scan can repopulate
            # them. The scan slot and its cooldown keep repeated toggles cheap.
            await self.start_scan()

    async def get_provider_settings(self) -> list[dict[str, Any]]:
        """Every declared provider with its switch position and catalogue size.

        Unlike the dashboard this lists disabled providers too — it is the screen the
        user switches them from, so hiding them here would lock them out. Retired
        models are counted separately, because they are hidden but still restorable.
        """
        enabled = await effective_enabled()
        health_rows = await get_all_health()
        online_keys = {
            (row["provider"], row["model_id"]) for row in health_rows if row["status"] == "online"
        }

        counts: dict[str, int] = {}
        online: dict[str, int] = {}
        for m in self._visible(self._get_all_models()):
            counts[m.provider] = counts.get(m.provider, 0) + 1
            if (m.provider, m.id) in online_keys:
                online[m.provider] = online.get(m.provider, 0) + 1

        retired: dict[str, int] = {}
        for provider_key, _model_id in self._retired:
            retired[provider_key] = retired.get(provider_key, 0) + 1

        return [
            {
                "key": key,
                "name": provider.name,
                "enabled": enabled.get(key, False),
                "default_enabled": provider.default_enabled,
                "model_count": counts.get(key, 0),
                "online_count": online.get(key, 0),
                "retired_count": retired.get(key, 0),
            }
            for key, provider in PROVIDERS.items()
        ]

    async def get_retired_models(self) -> list[dict[str, Any]]:
        """What the upstream stopped serving, and when we proved it.

        The dashboard hides these rows, so without a list of their own the operator
        could neither see what was dropped nor bring any of it back.
        """
        stored = await get_retirements()
        return [
            {
                "provider": provider_key,
                "provider_name": PROVIDERS[provider_key].name if provider_key in PROVIDERS else provider_key,
                "model_id": model_id,
                "retired_at": when,
            }
            for (provider_key, model_id), when in sorted(stored.items())
        ]

    async def restore_model(self, provider_key: str, model_id: str) -> None:
        """Undo a retirement. The next scan probes it again and re-judges the evidence."""
        await clear_retirement(provider_key, model_id)
        self._retired.discard((provider_key, model_id))

    async def get_dashboard_data(self) -> dict[str, Any]:
        """Combine the model catalog with the latest persisted health data.

        Read-only by design: discovery and probing are driven by the scheduler and
        explicit scan requests, never by a dashboard poll.

        Providers switched off in settings are left out entirely — the counts describe
        what the user is actually monitoring, not the whole catalogue. Models the
        upstream stopped serving are left out too; they stay restorable from settings.
        """
        enabled = await effective_enabled()
        models = [
            m for m in self._visible(self._get_all_models()) if enabled.get(m.provider, False)
        ]
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
