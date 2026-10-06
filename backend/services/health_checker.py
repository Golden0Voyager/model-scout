"""Lightweight health checker for model endpoints.

Replaces the heavy chat-completion benchmark with fast probes:
1. models_endpoint: HEAD/GET to the provider's /models endpoint (no token cost)
2. chat_ping: Send a single-token completion to test actual inference (minimal cost)
3. skip: No API key or unsupported
"""

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI

from core.config import ProviderConfig, get_provider_config


@dataclass
class ProbeResult:
    model_id: str
    provider: str
    status: str  # online | offline | unknown | error | no_key
    latency_ms: int | None = None
    error_message: str | None = None


# Only features the dashboard already knows how to label are translated; anything
# else would render as a raw snake_case tag.
_CAPABILITY_BY_FEATURE = {"tools": "function_calling", "reasoning": "reasoning"}

# ZenMux publishes capabilities as flags rather than a feature list.
_CAPABILITY_BY_FLAG = {
    "reasoning": "reasoning",
    "tools": "function_calling",
    "function_calling": "function_calling",
}


def _capabilities(raw: dict[str, Any], context_length: int) -> list[str]:
    """Derive capability tags from whichever schema this provider publishes."""
    capabilities = ["chat"]
    if (
        raw.get("supports_image_in")
        or raw.get("supports_vision")
        or "image" in (raw.get("input_modalities") or [])
    ):
        capabilities.append("vision")
    for feature in raw.get("supported_features") or []:
        mapped = _CAPABILITY_BY_FEATURE.get(feature)
        if mapped and mapped not in capabilities:
            capabilities.append(mapped)
    for flag, capability in _CAPABILITY_BY_FLAG.items():
        if (raw.get("capabilities") or {}).get(flag) and capability not in capabilities:
            capabilities.append(capability)
    for flag, capability in (("supports_tools", "function_calling"), ("supports_reasoning", "reasoning")):
        if raw.get(flag) and capability not in capabilities:
            capabilities.append(capability)
    if context_length >= 1_000_000:
        capabilities.append("long_context")
    return capabilities


def _base_tier(tiers: Any) -> dict[str, Any] | None:
    """The entry price of a tiered price list, ignoring volume break points."""
    if not isinstance(tiers, list) or not tiers:
        return None
    first = tiers[0]
    return first if isinstance(first, dict) else None


def _pricing_info(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a published price into the per-1M amount the catalog stores.

    Three shapes exist in the wild. OpenRouter and SenseNova give USD **per token** in
    a `pricing` object; TokenRhythm gives CNY **per 1M tokens** and keeps base and
    discounted rates apart; ZenMux gives USD **per 1M tokens** as a list of tiers.
    Reading any of them with another's multiplier is a million-fold error, so each
    branch states its own unit instead of the code assuming one.
    """
    effective_in = raw.get("effective_input_price_per_million")
    effective_out = raw.get("effective_output_price_per_million")
    if effective_in is not None and effective_out is not None:
        try:
            in_price = float(effective_in)
            out_price = float(effective_out)
        except (TypeError, ValueError):
            return {}
        info: dict[str, Any] = {
            # The rate the account actually pays, not the list price it never gets.
            "pricing_input_per_1m": in_price,
            "pricing_output_per_1m": out_price,
            "pricing_currency": raw.get("currency") or "USD",
            "is_free": in_price == 0.0 and out_price == 0.0,
        }
        if raw.get("has_discount"):
            info["pricing_note"] = "限时折扣"
        return info

    tiers = raw.get("pricings")
    if isinstance(tiers, dict):
        prompt = _base_tier(tiers.get("prompt"))
        completion = _base_tier(tiers.get("completion"))
        if not prompt or not completion or prompt.get("unit") != "perMTokens":
            return {}
        try:
            in_price = float(prompt["value"])
            out_price = float(completion["value"])
        except (KeyError, TypeError, ValueError):
            return {}
        return {
            "pricing_input_per_1m": in_price,
            "pricing_output_per_1m": out_price,
            "pricing_currency": prompt.get("currency") or "USD",
            "is_free": in_price == 0.0 and out_price == 0.0,
        }

    pricing = raw.get("pricing")
    if not isinstance(pricing, dict):
        return {}
    scale = 1_000_000.0 if "1m" not in str(pricing.get("unit") or "") else 1.0
    try:
        prompt_price = float(pricing.get("prompt", 0))
        completion_price = float(pricing.get("completion", 0))
    except (TypeError, ValueError):
        return {}
    return {
        "pricing_input_per_1m": prompt_price * scale,
        "pricing_output_per_1m": completion_price * scale,
        "pricing_currency": pricing.get("currency") or "USD",
        "is_free": prompt_price == 0.0 and completion_price == 0.0,
    }


# (model_ids, latency_ms, error_message) — a failed lookup is a value too, so it can
# be cached instead of re-fetched once per model.
ModelsSnapshot = tuple[set | None, int | None, str | None]
# The same snapshot before the ids are projected out of it. Rows are the superset: the
# discovery path needs their metadata and the probe path only needs membership, so both
# share one cached document.
RowsSnapshot = tuple[list | None, int | None, str | None]


def _model_ids(rows: list[Any]) -> set[str]:
    """Model ids as the provider spells them, from whichever field carries the name."""
    return {
        str(value)
        for value in (
            row.get("id") or row.get("name") for row in rows if isinstance(row, dict)
        )
        if value
    }


def _describe_error(error: Exception) -> str:
    """A failure reason that is never blank.

    Some transport errors — a connect timeout among them — stringify to "", and one layer
    up an empty string reads as "no error", so a timed-out catalogue reported "returned no
    data" instead of naming what actually happened.
    """
    return (str(error).strip() or type(error).__name__)[:120]

# How each provider says "this model is gone". These are terminal answers about the
# model itself, unlike a timeout, a 429 or an empty balance, which say nothing about
# whether the model exists and must never be read as a retirement.
_MODEL_MISSING_MARKERS = (
    "does not exist",
    "not exist",
    "not found",
    "unsupported model",
    "model_not_found",
    "unknown model",
)


def is_model_missing(message: str) -> bool:
    """True when a provider error means the model itself no longer exists."""
    lowered = message.lower()
    return any(marker in lowered for marker in _MODEL_MISSING_MARKERS)


class HealthChecker:
    def __init__(self, proxy: str | None = None):
        self._proxy = proxy
        # trust_env=False keeps routing decisions with the provider's `network` field:
        # with it left on, an ambient HTTPS_PROXY would silently pull the "direct"
        # (domestic) providers through the overseas proxy as well.
        self._proxy_client: httpx.AsyncClient = httpx.AsyncClient(
            proxy=proxy, timeout=15.0, follow_redirects=True, trust_env=False
        )
        self._direct_client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, trust_env=False
        )
        self._openai_clients: dict[str, AsyncOpenAI] = {}
        # provider_key -> (fetched_at_ms, snapshot of the raw catalogue rows)
        self._models_cache: dict[str, tuple[int, RowsSnapshot]] = {}
        # provider_key -> the fetch every concurrent caller should join
        self._models_inflight: dict[str, asyncio.Task[RowsSnapshot]] = {}
        # Must outlast a full scan (measured ~40s over ~490 models); a shorter TTL
        # expired mid-scan and made every later model re-fetch. Scans also reset the
        # cache explicitly, so this bound only governs ad-hoc single-model probes.
        self._cache_ttl_ms = 120_000

    async def __aenter__(self) -> "HealthChecker":
        return self

    async def __aexit__(self, *args) -> None:
        await self._proxy_client.aclose()
        await self._direct_client.aclose()
        for client in self._openai_clients.values():
            await client.close()
        self._models_cache.clear()

    def _get_http_client(self, provider: ProviderConfig) -> httpx.AsyncClient:
        return self._direct_client if provider.network == "direct" else self._proxy_client

    def _api_key(self, provider: ProviderConfig) -> str | None:
        """Return a usable key, or None when unset or left as a template placeholder."""
        value = os.getenv(provider.api_key_env, "").strip()
        if not value or value in ("***", "YOUR_API_KEY", "placeholder"):
            return None
        return value

    def _get_auth_headers(self, provider: ProviderConfig, api_key: str) -> dict[str, str]:
        if provider.auth_style == "api_key":
            return {"api-key": api_key}
        return {"Authorization": f"Bearer {api_key}"}

    def _get_openai_client(self, provider: ProviderConfig) -> AsyncOpenAI | None:
        if provider.key in self._openai_clients:
            return self._openai_clients[provider.key]

        api_key = self._api_key(provider)
        if not api_key:
            return None

        http_client = self._get_http_client(provider)
        extra_headers = {}
        if provider.auth_style == "api_key":
            extra_headers = {"api-key": api_key}
        client = AsyncOpenAI(
            base_url=provider.base_url,
            api_key=api_key,
            http_client=http_client,
            default_headers=extra_headers or None,
        )
        self._openai_clients[provider.key] = client
        return client

    def reset_model_cache(self) -> None:
        """Drop every cached catalogue so the next scan sees one coherent snapshot."""
        self._models_cache.clear()

    def listed_ids(self, provider_key: str) -> set[str] | None:
        """The catalogue this provider last returned, or None when that lookup failed.

        Retirement reads this. An id missing from a lookup that never succeeded is not
        evidence of anything, so a failed snapshot has to stay invisible to the caller.
        """
        cached = self._models_cache.get(provider_key)
        if cached is None:
            return None
        fetched_at, snapshot = cached
        if int(time.time() * 1000) - fetched_at >= self._cache_ttl_ms:
            return None
        rows = snapshot[0]
        return None if rows is None else _model_ids(rows)

    async def _provider_rows(self, provider: ProviderConfig) -> RowsSnapshot:
        """The provider's catalogue rows: cached, and shared by concurrent callers.

        One scan wants this document twice per provider — once to discover models, once
        to probe them — so it is fetched once. Asking a 150 KB catalogue twice, while
        six probes are already queued on the proxy, is how every ZenMux row ended up
        timing out and then reporting nothing at all.

        Failures are cached as readily as successes; otherwise a broken /models was
        re-requested once per model, and concurrent callers join one in-flight request.
        """
        cached = self._models_cache.get(provider.key)
        if cached is not None:
            fetched_at, snapshot = cached
            if int(time.time() * 1000) - fetched_at < self._cache_ttl_ms:
                return snapshot

        inflight = self._models_inflight.get(provider.key)
        if inflight is not None:
            return await asyncio.shield(inflight)

        task = asyncio.create_task(self._load_provider_rows(provider))
        self._models_inflight[provider.key] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._models_inflight.pop(provider.key, None)

    async def _load_provider_rows(self, provider: ProviderConfig) -> RowsSnapshot:
        api_key = self._api_key(provider)
        if api_key is None:
            snapshot: RowsSnapshot = (None, None, f"no API key ({provider.api_key_env})")
        else:
            client = self._get_http_client(provider)
            url = f"{provider.base_url}{provider.models_endpoint}"
            headers = self._get_auth_headers(provider, api_key)
            start = time.perf_counter()
            try:
                response = await client.get(url, headers=headers)
                latency_ms = int((time.perf_counter() - start) * 1000)
                if response.status_code == 200:
                    snapshot = (response.json().get("data", []), latency_ms, None)
                else:
                    snapshot = (None, latency_ms, f"HTTP {response.status_code}")
            except Exception as e:
                snapshot = (None, None, _describe_error(e))

        self._models_cache[provider.key] = (int(time.time() * 1000), snapshot)
        return snapshot

    async def _fetch_provider_models(self, provider: ProviderConfig) -> ModelsSnapshot:
        """The provider's model ids, read from the same document the discovery step used."""
        rows, latency_ms, error = await self._provider_rows(provider)
        if rows is None:
            return None, latency_ms, error
        return _model_ids(rows), latency_ms, error

    async def probe(self, model_id: str, provider_key: str) -> ProbeResult:
        """Run a lightweight probe for a single model."""
        provider = get_provider_config(provider_key)
        if not provider:
            return ProbeResult(
                model_id=model_id,
                provider=provider_key,
                status="error",
                error_message=f"Unknown provider: {provider_key}",
            )

        api_key = self._api_key(provider)
        if api_key is None:
            return ProbeResult(
                model_id=model_id,
                provider=provider_key,
                status="no_key",
                error_message=f"Missing API key ({provider.api_key_env})",
            )

        # Try models endpoint first (free, fast) - uses cache
        if provider.discovery == "dynamic" and provider.models_endpoint:
            model_ids, latency_ms, error = await self._fetch_provider_models(provider)
            if model_ids is not None:
                if model_id in model_ids:
                    return ProbeResult(
                        model_id=model_id,
                        provider=provider.key,
                        status="online",
                        latency_ms=latency_ms,
                    )
                # Model not in list — could be static-only model, fallback to chat
                ping_result = await self._probe_chat_ping(model_id, provider)
                if ping_result.status == "error" and not ping_result.error_message:
                    ping_result = ProbeResult(
                        model_id=ping_result.model_id,
                        provider=ping_result.provider,
                        status="error",
                        error_message=f"Chat ping failed (empty error) for {provider.key}/{model_id}",
                    )
                return ping_result

            # Models endpoint failed, fallback to chat ping
            if error:
                ping_result = await self._probe_chat_ping(model_id, provider)
                if ping_result.status == "error" and not ping_result.error_message:
                    ping_result = ProbeResult(
                        model_id=ping_result.model_id,
                        provider=ping_result.provider,
                        status="error",
                        error_message=f"Models endpoint failed ({error}), chat ping also failed empty",
                    )
                return ping_result
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                latency_ms=latency_ms,
                error_message=error or f"Models endpoint returned no data for {provider.key}",
            )

        # Static providers: use chat ping
        ping_result = await self._probe_chat_ping(model_id, provider)
        if ping_result.status == "error" and not ping_result.error_message:
            ping_result = ProbeResult(
                model_id=ping_result.model_id,
                provider=ping_result.provider,
                status="error",
                error_message=f"Static chat ping failed (empty error) for {provider.key}/{model_id}",
            )
        return ping_result

    async def _probe_chat_ping(
        self, model_id: str, provider: ProviderConfig
    ) -> ProbeResult:
        """Send a minimal chat completion to verify actual inference."""
        client = self._get_openai_client(provider)
        if not client:
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="no_key",
                error_message="No OpenAI client available",
            )

        # Skip non-chat models
        skip_keywords = ["guard", "classification", "rerank", "moderation", "embedding", "whisper", "vision-encoder"]
        if any(kw in model_id.lower() for kw in skip_keywords):
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="unknown",
                error_message="Non-chat model, skipped",
            )

        start = time.perf_counter()
        try:
            response = await client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=1,
                stream=False,
            )
            latency_ms = int((time.perf_counter() - start) * 1000)

            if response.choices and response.choices[0].message:
                return ProbeResult(
                    model_id=model_id,
                    provider=provider.key,
                    status="online",
                    latency_ms=latency_ms,
                )
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                latency_ms=latency_ms,
                error_message="Empty response",
            )
        except Exception as e:
            msg = str(e)
            if is_model_missing(msg):
                return ProbeResult(
                    model_id=model_id,
                    provider=provider.key,
                    status="offline",
                    error_message="Model not found at provider",
                )
            if "429" in msg:
                return ProbeResult(
                    model_id=model_id,
                    provider=provider.key,
                    status="error",
                    error_message="Rate limited (429)",
                )
            if "401" in msg or "403" in msg:
                return ProbeResult(
                    model_id=model_id,
                    provider=provider.key,
                    status="error",
                    error_message="Auth failed",
                )
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                error_message=msg[:120],
            )

    async def discover_models(self, provider_key: str) -> tuple[list[str] | None, str | None]:
        """Discover available model IDs from a dynamic provider."""
        provider = get_provider_config(provider_key)
        if not provider:
            return None, f"Unknown provider: {provider_key}"
        if provider.discovery != "dynamic" or not provider.models_endpoint:
            return None, "Provider does not support dynamic discovery"

        model_ids, _, error = await self._fetch_provider_models(provider)
        if model_ids is not None:
            return sorted(model_ids), None
        return None, error

    async def discover_models_detailed(
        self, provider_key: str
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Discover models with metadata (id, name, pricing, etc.)."""
        provider = get_provider_config(provider_key)
        if not provider:
            return None, f"Unknown provider: {provider_key}"
        if provider.discovery != "dynamic" or not provider.models_endpoint:
            return None, "Provider does not support dynamic discovery"

        rows, _latency_ms, error = await self._provider_rows(provider)
        if rows is None:
            return None, error

        results: list[dict[str, Any]] = []
        for m in rows:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("name")
            if not mid:
                continue
            info: dict[str, Any] = {
                "id": mid,
                "name": m.get("name") or m.get("display_name") or mid,
            }
            context_length = int(m.get("context_length") or 0)
            if context_length:
                info["context_length"] = context_length
            max_output = m.get("max_output_length") or m.get("max_completion_tokens")
            if max_output:
                info["max_output_tokens"] = int(max_output)
            if m.get("description"):
                info["description"] = m["description"]
            info["capabilities"] = _capabilities(m, context_length)
            # Aggregators list image, video, TTS and embedding models beside the chat
            # ones. The sync layer needs the declared outputs to tell them apart.
            info["output_modalities"] = [str(v) for v in (m.get("output_modalities") or [])]
            info.update(_pricing_info(m))
            results.append(info)
        return results, None

    async def probe_batch(
        self, probes: list[dict[str, str]], concurrency: int = 8
    ) -> list[ProbeResult]:
        """Probe multiple models with controlled concurrency."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _wrapped(p: dict[str, str]) -> ProbeResult:
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        self.probe(p["model_id"], p["provider"]),
                        timeout=20.0,
                    )
                except TimeoutError:
                    return ProbeResult(
                        model_id=p["model_id"],
                        provider=p["provider"],
                        status="error",
                        error_message="Probe timeout (20s)",
                    )
                await asyncio.sleep(0.15)  # be polite to APIs
                return result

        tasks = [_wrapped(p) for p in probes]
        return await asyncio.gather(*tasks)
