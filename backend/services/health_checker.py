"""Lightweight health checker for model endpoints.

Replaces the heavy chat-completion benchmark with fast probes:
1. models_endpoint: HEAD/GET to the provider's /models endpoint (no token cost)
2. chat_ping: Send a single-token completion to test actual inference (minimal cost)
3. skip: No API key or unsupported
"""

import os
import time
import asyncio
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI

from core.config import get_provider_config, ProviderConfig


@dataclass
class ProbeResult:
    model_id: str
    provider: str
    status: str  # online | offline | unknown | error | no_key
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None


class HealthChecker:
    def __init__(self, proxy: Optional[str] = None):
        self._proxy = proxy
        self._proxy_client: Optional[httpx.AsyncClient] = None
        self._direct_client: Optional[httpx.AsyncClient] = None
        self._openai_clients: Dict[str, AsyncOpenAI] = {}
        # Cache for provider model lists: provider_key -> (fetch_time_ms, models_set, latency_ms)
        self._models_cache: Dict[str, Tuple[int, set, Optional[int]]] = {}
        self._cache_ttl_ms = 30000  # 30s cache for models endpoint

    async def __aenter__(self):
        self._proxy_client = httpx.AsyncClient(
            proxy=self._proxy, timeout=15.0, follow_redirects=True
        )
        self._direct_client = httpx.AsyncClient(
            timeout=15.0, follow_redirects=True
        )
        return self

    async def __aexit__(self, *args):
        if self._proxy_client:
            await self._proxy_client.aclose()
        if self._direct_client:
            await self._direct_client.aclose()
        for client in self._openai_clients.values():
            await client.close()
        self._models_cache.clear()

    def _get_http_client(self, provider: ProviderConfig) -> httpx.AsyncClient:
        return self._direct_client if provider.network == "direct" else self._proxy_client

    def _get_auth_headers(self, provider: ProviderConfig) -> Dict[str, str]:
        api_key = os.getenv(provider.api_key_env, "")
        if provider.auth_style == "api_key":
            return {"api-key": api_key}
        return {"Authorization": f"Bearer {api_key}"}

    def _get_openai_client(self, provider: ProviderConfig) -> Optional[AsyncOpenAI]:
        if provider.key in self._openai_clients:
            return self._openai_clients[provider.key]

        api_key = os.getenv(provider.api_key_env)
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

    async def _fetch_provider_models(self, provider: ProviderConfig) -> Tuple[Optional[set], Optional[int], Optional[str]]:
        """Fetch the provider's model list, with caching.
        Returns (model_ids_set, latency_ms, error_message)."""
        now = int(time.time() * 1000)
        cached = self._models_cache.get(provider.key)
        if cached:
            cached_time, cached_set, cached_latency = cached
            if now - cached_time < self._cache_ttl_ms:
                return cached_set, cached_latency, None

        client = self._get_http_client(provider)
        url = f"{provider.base_url}{provider.models_endpoint}"
        headers = self._get_auth_headers(provider)

        start = time.perf_counter()
        try:
            response = await client.get(url, headers=headers)
            latency_ms = int((time.perf_counter() - start) * 1000)

            if response.status_code == 200:
                data = response.json()
                models = data.get("data", [])
                model_ids = {m.get("id") or m.get("name") for m in models if m.get("id") or m.get("name")}
                self._models_cache[provider.key] = (now, model_ids, latency_ms)
                return model_ids, latency_ms, None

            return None, latency_ms, f"HTTP {response.status_code}"
        except Exception as e:
            return None, None, str(e)[:120]

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

        api_key = os.getenv(provider.api_key_env)
        if not api_key or api_key.strip() in ("", "***", "YOUR_API_KEY", "placeholder"):
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
            if "does not exist" in msg or "not found" in msg.lower():
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

    async def _fetch_provider_models_raw(self, provider: ProviderConfig) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """Fetch the provider's raw model list (with metadata). No caching."""
        client = self._get_http_client(provider)
        url = f"{provider.base_url}{provider.models_endpoint}"
        headers = self._get_auth_headers(provider)
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                models = data.get("data", [])
                return models, None
            return None, f"HTTP {response.status_code}"
        except Exception as e:
            return None, str(e)[:120]

    async def discover_models(self, provider_key: str) -> Tuple[Optional[List[str]], Optional[str]]:
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
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """Discover models with metadata (id, name, pricing, etc.)."""
        provider = get_provider_config(provider_key)
        if not provider:
            return None, f"Unknown provider: {provider_key}"
        if provider.discovery != "dynamic" or not provider.models_endpoint:
            return None, "Provider does not support dynamic discovery"

        models, error = await self._fetch_provider_models_raw(provider)
        if models is None:
            return None, error

        results: List[Dict[str, Any]] = []
        for m in models:
            mid = m.get("id") or m.get("name")
            if not mid:
                continue
            info: Dict[str, Any] = {
                "id": mid,
                "name": m.get("name") or mid,
            }
            # Moonshot metadata
            if "context_length" in m:
                info["context_length"] = m["context_length"]
            if m.get("supports_image_in"):
                info["capabilities"] = ["chat", "vision"]
            else:
                info["capabilities"] = ["chat"]
            # OpenRouter pricing
            pricing = m.get("pricing")
            if isinstance(pricing, dict):
                try:
                    prompt_price = float(pricing.get("prompt", 0))
                    completion_price = float(pricing.get("completion", 0))
                    info["pricing_input_per_1m"] = prompt_price * 1_000_000
                    info["pricing_output_per_1m"] = completion_price * 1_000_000
                    info["is_free"] = prompt_price == 0.0 and completion_price == 0.0
                except (ValueError, TypeError):
                    pass
            results.append(info)
        return results, None

    async def probe_batch(
        self, probes: List[Dict[str, str]], concurrency: int = 8
    ) -> List[ProbeResult]:
        """Probe multiple models with controlled concurrency."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _wrapped(p: Dict[str, str]) -> ProbeResult:
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        self.probe(p["model_id"], p["provider"]),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
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
