"""Lightweight health checker for model endpoints.

Replaces the heavy chat-completion benchmark with fast probes:
1. models_endpoint: HEAD/GET to the provider's /models endpoint (no token cost)
2. chat_ping: Send a single-token completion to test actual inference (minimal cost)
3. skip: No API key or unsupported
"""

import asyncio
import base64
import io
import os
import time
import wave
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI

from core.config import ProviderConfig, get_provider_config


@dataclass
class ProbeResult:
    model_id: str
    provider: str
    status: str  # online | offline | unknown | error | no_key | restricted
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


def _modalities(raw: dict[str, Any], field: str) -> list[str]:
    """Declared modalities, wherever this provider puts them.

    OpenRouter publishes none of this at the top level — all 458 of its rows keep it
    under `architecture` — so reading only the flat field made every one of its models
    look text-only, and hid its image and audio generators from the non-chat filter.
    """
    value = raw.get(field)
    if not value and isinstance(raw.get("architecture"), dict):
        value = raw["architecture"].get(field)
    return [str(v) for v in (value or [])]


def _capabilities(raw: dict[str, Any], context_length: int) -> list[str]:
    """Derive capability tags from whichever schema this provider publishes."""
    capabilities = ["chat"]
    if (
        raw.get("supports_image_in")
        or raw.get("supports_vision")
        or "image" in _modalities(raw, "input_modalities")
    ):
        capabilities.append("vision")
    for feature in raw.get("supported_features") or []:
        mapped = _CAPABILITY_BY_FEATURE.get(feature)
        if mapped and mapped not in capabilities:
            capabilities.append(mapped)
    for flag, capability in _CAPABILITY_BY_FLAG.items():
        if (raw.get("capabilities") or {}).get(flag) and capability not in capabilities:
            capabilities.append(capability)
    # TokenHarbor calls the tool-support flag `tool_call`/`function_call`.
    for flag, capability in (
        ("supports_tools", "function_calling"),
        ("supports_reasoning", "reasoning"),
        ("tool_call", "function_calling"),
    ):
        if raw.get(flag) and capability not in capabilities:
            capabilities.append(capability)
    if context_length >= 1_000_000:
        capabilities.append("long_context")
    return capabilities


def _base_tier(tiers: Any) -> dict[str, Any] | None:
    """The cheapest entry in a tiered price list, however the provider ordered it.

    Tiers are volume break points, so the headline a monitor should show is "from $X" —
    the lowest rate. Picking index 0 trusted the list to arrive sorted, which ZenMux
    does today but is not something its schema promises.
    """
    if not isinstance(tiers, list) or not tiers:
        return None

    candidates = [tier for tier in tiers if isinstance(tier, dict)]
    if not candidates:
        return None

    def _value(tier: dict[str, Any]) -> float:
        try:
            return float(tier["value"])
        except (KeyError, TypeError, ValueError):
            return float("inf")

    return min(candidates, key=_value)


def _pricing_info(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a published price into the per-1M amount the catalog stores.

    Four shapes exist in the wild. OpenRouter and SenseNova give USD **per token** in
    a `pricing` object; TokenRhythm gives CNY **per 1M tokens** and keeps base and
    discounted rates apart; ZenMux gives USD **per 1M tokens** as a list of tiers;
    TokenHarbor gives USD **per 1M tokens** as input_usd_per_1m with no `prompt` key.
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
        # Both sides must be stated in per-1M tokens, and in one currency: mixing a
        # per-token input rate with a per-million output rate, or USD with CNY, would
        # publish a price pair that no request can actually be billed at.
        if (
            not prompt
            or not completion
            or prompt.get("unit") != "perMTokens"
            or completion.get("unit") != "perMTokens"
            or (
                prompt.get("currency")
                and completion.get("currency")
                and prompt.get("currency") != completion.get("currency")
            )
        ):
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
    # TokenHarbor publishes USD per 1M directly as input_usd_per_1m/output_usd_per_1m
    # and omits `prompt` entirely — the generic branch below would read both sides as
    # absent and publish every paid model as free.
    if "input_usd_per_1m" in pricing or "output_usd_per_1m" in pricing:
        try:
            in_price = float(pricing.get("input_usd_per_1m", 0))
            out_price = float(pricing.get("output_usd_per_1m", 0))
        except (TypeError, ValueError):
            return {}
        return {
            "pricing_input_per_1m": in_price,
            "pricing_output_per_1m": out_price,
            "pricing_currency": "USD",
            "is_free": in_price == 0.0 and out_price == 0.0,
        }
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


def _audio_kind(model_id: str) -> str | None:
    """ASR/TTS ids are called through chat/completions on MiMo, not /audio/*."""
    lower = model_id.lower()
    if lower.endswith("-asr") or "-asr-" in lower:
        return "asr"
    if "-tts" in lower:
        return "tts"
    return None


def _silence_wav_data_url() -> str:
    """0.25s of 16kHz silence, base64 data URL — the cheapest valid ASR input."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 4000)
    return "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


# Voice cloning needs a reference sample. Synthesising one once per process keeps the
# daily probe at two calls instead of shipping a binary sample in the repo.
_REFERENCE_VOICE: str | None = None


class HealthChecker:
    def __init__(self, proxy: str | None = None):
        self._proxy = proxy
        self._retry_delay = 1.0
        self._recycle_transports = True
        self._openai_clients: dict[str, AsyncOpenAI] = {}
        # provider_key -> (fetched_at_ms, snapshot of the raw catalogue rows)
        self._models_cache: dict[str, tuple[int, RowsSnapshot]] = {}
        # provider_key -> the fetch every concurrent caller should join
        self._models_inflight: dict[str, asyncio.Task[RowsSnapshot]] = {}
        # Must outlast a full scan (measured ~40s over ~490 models); a shorter TTL
        # expired mid-scan and made every later model re-fetch. Scans also reset the
        # cache explicitly, so this bound only governs ad-hoc single-model probes.
        self._cache_ttl_ms = 120_000
        self._discovery_client_factory = self._make_discovery_client
        self._build_clients()

    def _build_clients(self) -> None:
        # trust_env=False keeps routing decisions with the provider's `network` field:
        # with it left on, an ambient HTTPS_PROXY would silently pull the "direct"
        # (domestic) providers through the overseas proxy as well.
        #
        # No keep-alive on purpose. Through the local proxy a reused connection can be
        # dead on arrival (the peer sends FIN, the next request hangs until read
        # timeout), and every such failure leaked a socket instead of freeing the pool
        # slot. Each request gets its own connection now; TLS setup is the cheaper cost.
        probe_limits = httpx.Limits(max_connections=32, max_keepalive_connections=0)
        self._proxy_client: httpx.AsyncClient = httpx.AsyncClient(
            proxy=self._proxy, timeout=15.0, follow_redirects=True, trust_env=False, limits=probe_limits
        )
        self._direct_client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, trust_env=False, limits=probe_limits
        )

    def _make_discovery_client(self, provider: ProviderConfig) -> httpx.AsyncClient:
        """One-shot client for a single catalogue attempt.

        Catalogues used to share a pool with each other only, but one hung connection
        still pinned a slot for the rest of the scan and surfaced as PoolTimeout. A
        fresh client per attempt costs one TLS handshake and cannot be starved.
        """
        return httpx.AsyncClient(
            proxy=self._proxy if provider.network != "direct" else None,
            timeout=httpx.Timeout(15.0, connect=8.0, pool=5.0),
            follow_redirects=True,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )

    async def reset_transport(self) -> None:
        """Drop and rebuild the probe pool at the start of a scan.

        The local proxy resets TLS under load, and httpcore can leave those sockets in
        CLOSED state still counted against the pool. A long-running process therefore
        wedges after a few bad scans while a fresh process works. Rebuilding once per
        scan bounds that to the scan that caused it.
        """
        if not self._recycle_transports:
            return
        for client in self._openai_clients.values():
            await client.close()
        self._openai_clients.clear()
        await self._proxy_client.aclose()
        await self._direct_client.aclose()
        self._build_clients()

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
        headers = (
            {"api-key": api_key}
            if provider.auth_style == "api_key"
            else {"Authorization": f"Bearer {api_key}"}
        )
        if provider.user_agent:
            headers["User-Agent"] = provider.user_agent
        return headers

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
        if provider.user_agent:
            extra_headers["User-Agent"] = provider.user_agent
        client = AsyncOpenAI(
            base_url=provider.base_url,
            api_key=api_key,
            http_client=http_client,
            default_headers=extra_headers or None,
            max_retries=0,
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
            url = f"{provider.base_url}{provider.models_endpoint}"
            snapshot = await self._fetch_rows_with_retry(
                provider,
                url,
                self._get_auth_headers(provider, api_key),
            )

        self._models_cache[provider.key] = (int(time.time() * 1000), snapshot)
        return snapshot

    async def _fetch_rows_with_retry(
        self, provider: ProviderConfig, url: str, headers: dict[str, str]
    ) -> RowsSnapshot:
        """Three attempts: a flaky proxy must not read as 'this provider has no models'.

        Client errors are final — 401/404 mean something about the key or the route,
        not about the network. Transport errors and 5xx get retried.
        """
        attempts = 3
        for attempt in range(attempts):
            start = time.perf_counter()
            try:
                async with self._discovery_client_factory(provider) as client:
                    response = await client.get(url, headers=headers)
            except Exception as e:
                if attempt < attempts - 1:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                    continue
                return (None, None, _describe_error(e))
            latency_ms = int((time.perf_counter() - start) * 1000)
            if response.status_code == 200:
                return (response.json().get("data", []), latency_ms, None)
            if response.status_code >= 500 and attempt < attempts - 1:
                await asyncio.sleep(self._retry_delay * (attempt + 1))
                continue
            return (None, latency_ms, f"HTTP {response.status_code}")
        return (None, None, "discovery failed")

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

        # Audio models are listed like chat models, but "listed" is not "answers":
        # the daily probe must actually synthesise/transcribe something.
        kind = _audio_kind(model_id)
        if kind and provider.include_audio_models:
            return await self._probe_audio_ping(model_id, provider, kind, api_key)

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
                if "exceeded your current quota" in msg.lower():
                    # The billing-refusal signature phrase — distinct from
                    # recoverable RPM/TPM limits, which also mention quota but
                    # recover in seconds. This never recovers on its own: the
                    # account needs billing, not time.
                    return ProbeResult(
                        model_id=model_id,
                        provider=provider.key,
                        status="restricted",
                        error_message="Plan quota exceeded (needs billing)",
                    )
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

    async def _reference_voice(self, provider: ProviderConfig, api_key: str) -> str | None:
        """One short synthesised sample, reused by every voice-clone probe."""
        global _REFERENCE_VOICE
        if _REFERENCE_VOICE:
            return _REFERENCE_VOICE
        headers = self._get_auth_headers(provider, api_key)
        headers["Content-Type"] = "application/json"
        payload = {
            "model": "mimo-v2.5-tts",
            "messages": [
                {"role": "user", "content": "Say hello briefly."},
                {"role": "assistant", "content": "Hi there."},
            ],
            "audio": {"format": "wav", "voice": "mimo_default"},
        }
        try:
            response = await self._get_http_client(provider).post(
                f"{provider.base_url}/chat/completions", headers=headers, json=payload, timeout=45.0
            )
            data = response.json()["choices"][0]["message"]["audio"]["data"]
        except Exception:
            return None
        if not data:
            return None
        _REFERENCE_VOICE = "data:audio/wav;base64," + data
        return _REFERENCE_VOICE

    async def _probe_audio_ping(
        self, model_id: str, provider: ProviderConfig, kind: str, api_key: str
    ) -> ProbeResult:
        """One real ASR/TTS call through the chat endpoint.

        MiMo exposes both as chat completions: the assistant message is the text to
        speak, an input_audio part is the audio to transcribe. Being listed in
        /models is not evidence that either call works.
        """
        headers = self._get_auth_headers(provider, api_key)
        headers["Content-Type"] = "application/json"
        if kind == "tts":
            lower = model_id.lower()
            if lower.endswith("voiceclone"):
                reference = await self._reference_voice(provider, api_key)
                if not reference:
                    return ProbeResult(
                        model_id=model_id,
                        provider=provider.key,
                        status="error",
                        error_message="Could not synthesise a reference voice",
                    )
                payload: dict[str, Any] = {
                    "model": model_id,
                    "messages": [
                        {"role": "user", "content": ""},
                        {"role": "assistant", "content": "Hi."},
                    ],
                    "audio": {"format": "wav", "voice": reference},
                }
            elif lower.endswith("voicedesign"):
                payload = {
                    "model": model_id,
                    "messages": [
                        {"role": "user", "content": "A calm, warm adult voice speaking slowly."},
                        {"role": "assistant", "content": "Hi."},
                    ],
                    "audio": {"format": "wav"},
                }
            else:
                payload = {
                    "model": model_id,
                    "messages": [
                        {"role": "user", "content": "Say hello briefly."},
                        {"role": "assistant", "content": "Hi."},
                    ],
                    "audio": {"format": "wav", "voice": "mimo_default"},
                }
        else:
            payload = {
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_audio", "input_audio": {"data": _silence_wav_data_url()}}
                        ],
                    }
                ],
                "asr_options": {"language": "auto"},
            }

        start = time.perf_counter()
        try:
            response = await self._get_http_client(provider).post(
                f"{provider.base_url}/chat/completions", headers=headers, json=payload, timeout=45.0
            )
        except Exception as e:
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                error_message=_describe_error(e),
            )
        latency_ms = int((time.perf_counter() - start) * 1000)

        if response.status_code != 200:
            message = response.text[:200]
            if is_model_missing(message):
                return ProbeResult(
                    model_id=model_id,
                    provider=provider.key,
                    status="offline",
                    error_message="Model not found at provider",
                )
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                latency_ms=latency_ms,
                error_message=f"Error code: {response.status_code} - {message}",
            )

        try:
            body = response.json()
        except ValueError:
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                latency_ms=latency_ms,
                error_message="Non-JSON response",
            )
        message = ((body.get("choices") or [{}])[0].get("message") or {})
        if kind == "tts" and not (message.get("audio") or {}).get("data"):
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                latency_ms=latency_ms,
                error_message="Empty audio response",
            )
        if kind == "asr" and message.get("content") is None:
            return ProbeResult(
                model_id=model_id,
                provider=provider.key,
                status="error",
                latency_ms=latency_ms,
                error_message="Empty transcription",
            )
        return ProbeResult(
            model_id=model_id,
            provider=provider.key,
            status="online",
            latency_ms=latency_ms,
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
                # ZenMux calls it display_name, TokenHarbor calls it label.
                "name": m.get("name") or m.get("display_name") or m.get("label") or mid,
            }
            # DeepSeek publishes `context_window`, not `context_length`.
            context_length = int(m.get("context_length") or m.get("context_window") or 0)
            if context_length:
                info["context_length"] = context_length
            max_output = (
                m.get("max_output_length")
                or m.get("max_completion_tokens")
                or m.get("max_output_tokens")
            )
            if max_output:
                info["max_output_tokens"] = int(max_output)
            # TokenHarbor calls its one-line summary `blurb`, not `description`.
            if m.get("description") or m.get("blurb"):
                info["description"] = m["description"] if m.get("description") else m["blurb"]
            info["capabilities"] = _capabilities(m, context_length)
            # Aggregators list image, video, TTS and embedding models beside the chat
            # ones. The sync layer needs the declared outputs to tell them apart.
            info["output_modalities"] = _modalities(m, "output_modalities")
            info.update(_pricing_info(m))
            results.append(info)
        return results, None

    async def probe_batch(
        self, probes: list[dict[str, str]], concurrency: int = 8
    ) -> list[ProbeResult]:
        """Probe multiple models with controlled concurrency."""
        semaphore = asyncio.Semaphore(concurrency)
        # Audio calls are heavier and rate-sensitive; four of them next to each other
        # can starve each other. They run once a day, so serialise them.
        audio_semaphore = asyncio.Semaphore(1)

        async def _wrapped(p: dict[str, str]) -> ProbeResult:
            kind = _audio_kind(p["model_id"])
            gate = audio_semaphore if kind else semaphore
            async with gate:
                # Audio probes synthesise or transcribe real audio; voice cloning adds a
                # reference generation. Once a day, a longer ceiling costs nothing.
                timeout = 90.0 if kind else 20.0
                try:
                    result = await asyncio.wait_for(
                        self.probe(p["model_id"], p["provider"]),
                        timeout=timeout,
                    )
                except TimeoutError:
                    return ProbeResult(
                        model_id=p["model_id"],
                        provider=p["provider"],
                        status="error",
                        error_message=f"Probe timeout ({int(timeout)}s)",
                    )
                await asyncio.sleep(0.15)  # be polite to APIs
                return result

        tasks = [_wrapped(p) for p in probes]
        # Rebuild the pool every ~120 probes. A scan is long enough (hundreds of
        # requests through a flaky proxy) that leaked sockets would otherwise exhaust
        # it midway and turn the tail of the scan into timeouts.
        chunk_size = 120
        results: list[ProbeResult] = []
        for start in range(0, len(probes), chunk_size):
            chunk = tasks[start : start + chunk_size]
            results.extend(await asyncio.gather(*chunk))
            if start + chunk_size < len(probes):
                await self.reset_transport()
        return results
