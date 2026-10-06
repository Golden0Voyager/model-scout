"""Backend tests for ModelScout.

Several of these lock in regressions for defects found in review: outbound network
calls on the read path, model IDs containing slashes, non-atomic scan state, and key
material in the startup log.
"""

import asyncio
import importlib
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

import api.routes
from api.routes import router
from core import database
from core.access import (
    ALLOWED_ORIGINS,
    PROBE_MAX_CALLS,
    SlidingWindowLimiter,
    is_trusted_origin,
    probe_limiter,
)
from core.config import (
    FALLBACK_CNY_PER_USD,
    PROVIDERS,
    STATIC_MODELS,
    ModelConfig,
    get_models_for_provider,
    get_provider_config,
    get_static_models,
    provider_default_enabled,
)
from core.provider_state import effective_enabled, is_enabled
from services.fx import SOURCES, FxRate
from services.health_checker import HealthChecker, ProbeResult, is_model_missing
from services.sync_service import SyncService

_LOOP: asyncio.AbstractEventLoop | None = None


def run(coro: Any) -> Any:
    """Drive every coroutine in this suite on one shared event loop.

    Each test used to call run() separately, which means the pooled aiosqlite
    connection was opened in one loop and closed in another; aiosqlite resolves its
    futures on the loop captured at construction, so the close never returned and
    pytest hung in fixture teardown on Linux. One loop for the suite removes the class.
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
PROVIDER_KEYS = {p.api_key_env for p in PROVIDERS.values()}


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Point DB_PATH at a throwaway file for the duration of one test.

    Nothing to release: each operation opens and closes its own connection, so
    repointing the module attribute is sufficient and cannot strand a handle on a
    dead event loop.
    """
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test.db")
    run(database.init_db())
    yield database.DB_PATH
    database.DB_PATH = original


@pytest.fixture(autouse=True)
def fresh_probe_budget():
    """The limiter is process-global, so tests must not inherit each other's budget."""
    probe_limiter.reset()
    yield
    probe_limiter.reset()


@pytest.fixture(autouse=True)
def unwired_fx():
    api.routes.fx_rate = None
    yield
    api.routes.fx_rate = None


def _stub_service(urls: list[str] | None = None) -> SyncService:
    """A SyncService whose checker talks to a recording transport instead of the internet."""
    recorded = [] if urls is None else urls

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    service = SyncService()
    checker = HealthChecker()
    checker._direct_client = httpx.AsyncClient(transport=transport)
    checker._proxy_client = httpx.AsyncClient(transport=transport)
    service._checker = checker
    service.recorded_requests = recorded  # type: ignore[attr-defined]
    return service


class Api:
    """The router under test, driven on the suite's own event loop.

    Starlette's TestClient runs the app on a separate anyio portal loop, which means
    the pooled SQLite connection is created there and can never be closed from this
    loop — its worker thread then reports results onto a dead loop. ASGITransport
    keeps every coroutine, and therefore every connection, on one loop.
    """

    def __init__(self, service: SyncService | None) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api")
        api.routes.sync_service = service
        self._transport = ASGITransport(app=app)

    def _call(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None,
        body: dict[str, Any] | None = None,
    ):
        async def request() -> httpx.Response:
            async with httpx.AsyncClient(transport=self._transport, base_url="http://dashboard") as client:
                return await client.request(method, path, headers=headers or {}, json=body)

        return run(request())

    def get(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return self._call("GET", path, headers)

    def post(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return self._call("POST", path, headers)

    def put(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._call("PUT", path, None, body)

    def delete(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return self._call("DELETE", path, headers)


def _client(service: SyncService | None) -> Api:
    return Api(service)


def _service_with_payload(
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: int = 200,
) -> SyncService:
    """A SyncService whose providers all answer /models with the given catalog.

    Keys must exist for the providers under test, since discovery short-circuits on a
    missing key rather than sending an empty Authorization header.
    """
    for env_name in ("MOONSHOT_API_KEY", "SENSENOVA_API_KEY", "AGENTROUTER_API_KEY", "MIMO_TOKEN_PLAN_KEY"):
        monkeypatch.setenv(env_name, "test-key")
    recorded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(str(request.url))
        return httpx.Response(status, json=payload)

    transport = httpx.MockTransport(handler)
    service = SyncService()
    checker = HealthChecker()
    checker._direct_client = httpx.AsyncClient(transport=transport)
    checker._proxy_client = httpx.AsyncClient(transport=transport)
    service._checker = checker
    service.recorded_requests = recorded  # type: ignore[attr-defined]
    return service


# SenseNova publishes OpenRouter-shaped metadata; sampled from the live endpoint.
RICH_CATALOG: dict[str, Any] = {
    "data": [
        {
            "id": "sensenova-6.8-flash-lite",
            "name": "sensenova-6.8-flash-lite",
            "context_length": 262144,
            "max_output_length": 65536,
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "supported_features": ["tools", "json_mode", "reasoning"],
            "pricing": {"prompt": "0", "completion": "0"},
            "description": "Lightweight multimodal agent model.",
        },
        {
            "id": "deepseek-v4-pro",
            "name": "deepseek-v4-pro",
            "context_length": 1048576,
            "max_output_length": 65536,
            "input_modalities": ["text"],
            "supported_features": ["tools", "reasoning"],
            "pricing": {"prompt": "0.02", "completion": "0.05"},
        },
    ]
}


# ---------------------------------------------------------------- provider config


def test_sensenova_uses_the_live_token_endpoint() -> None:
    provider = get_provider_config("sensenova")
    assert provider is not None
    assert provider.base_url == "https://token.sensenova.cn/v1"
    assert provider.rich_discovery is True


def test_retired_sensenova_catalog_is_not_pinned_in_config() -> None:
    """The hosted-DeepSeek list disappeared with the old endpoint; nothing should pin it."""
    assert [m.id for m in get_models_for_provider("sensenova")] == []


def test_tokenrhythm_matches_its_published_access_details() -> None:
    """Docs specify https://tokenrhythm.studio/v1 with `Authorization: Bearer sk_xxx`."""
    provider = get_provider_config("tokenrhythm")
    assert provider is not None
    assert provider.base_url == "https://tokenrhythm.studio/v1"
    assert provider.api_key_env == "TOKENRHYTHM_API_KEY"
    assert provider.auth_style == "bearer"
    # Measured from this machine: the host answers unproxied, so it must not be
    # routed through the overseas proxy.
    assert provider.network == "direct"
    assert provider.discovery == "dynamic"
    assert provider.auto_discover is True
    # Measured: its /models carries context, max output, per-1M prices and per-model
    # capability booleans, so the rich path is the one that keeps real metadata.
    assert provider.rich_discovery is True


def test_tokenrhythm_catalogue_is_not_pinned_in_config() -> None:
    """What the account can call is an upstream fact, not a repo fact.

    The published model page carries 25 entries, two of which bill per generated
    image; pinning any of them would freeze a snapshot the provider never promised
    to keep, and would put image generators into a chat monitor.
    """
    assert [m.id for m in get_models_for_provider("tokenrhythm")] == []


def test_rich_discovery_drives_metadata_not_provider_names() -> None:
    """The rich path must be selected by config, not by an `if provider_key == ...` arm."""
    import inspect

    import services.sync_service as module

    source = inspect.getsource(module.SyncService._refresh_discovered_models)
    assert 'provider_key == "moonshot"' not in source
    # OpenRouter used to be a name branch with its own row builder; the free_only flag
    # replaced it, so nothing may reintroduce a per-provider condition here.
    assert 'provider_key == "openrouter"' not in source
    assert "free_only" in source
    for key in ("moonshot", "sensenova", "tokenrhythm", "openrouter", "zenmux"):
        provider = PROVIDERS[key]
        assert provider.rich_discovery is True


def test_free_only_needs_the_rich_path() -> None:
    """A bare model ID cannot tell free from paid, so the flag would silently no-op."""
    for key, provider in PROVIDERS.items():
        if provider.free_only:
            assert provider.rich_discovery is True, key


# ---------------------------------------------------------------- discovery mapping


def test_rich_catalog_populates_every_advertised_field(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    by_id = {m.id: m for m in service._discovered_models if m.provider == "sensenova"}
    lite = by_id["sensenova-6.8-flash-lite"]
    assert lite.context_length == 262144
    assert lite.max_output_tokens == 65536
    assert set(lite.capabilities) == {"chat", "vision", "function_calling", "reasoning"}
    assert "long_context" not in lite.capabilities
    assert lite.is_free is True
    assert lite.description == "Lightweight multimodal agent model."

    pro = by_id["deepseek-v4-pro"]
    assert pro.capabilities.count("long_context") == 1
    assert "vision" not in pro.capabilities
    assert pro.is_free is False
    assert pro.pricing_input_per_1m == 20_000.0
    assert pro.pricing_output_per_1m == 50_000.0


def test_unknown_features_do_not_leak_raw_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """json_mode has no dashboard label, so it must not surface as a raw snake_case tag."""
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))
    caps = {c for m in service._discovered_models for c in m.capabilities}
    assert "json_mode" not in caps
    assert not any("_" in c and c not in {"function_calling", "long_context"} for c in caps)


def test_moonshot_still_discovers_through_the_generalised_path(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))
    moonshot = [m for m in service._discovered_models if m.provider == "moonshot"]
    assert {m.id for m in moonshot} == {"sensenova-6.8-flash-lite", "deepseek-v4-pro"}


# Transcribed from the live https://tokenrhythm.studio/v1/models on 2026-10-05. Note that
# prices are per 1M tokens and that base and discounted rates are published separately.
TOKENRHYTHM_CATALOG: dict[str, Any] = {
    "data": [
        {
            "id": "glm-5.1",
            "owned_by": "tokenrhythm",
            "context_length": 200000,
            "max_completion_tokens": 128000,
            "currency": "CNY",
            "pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "prompt": "8.00000000",
                "completion": "28.00000000",
            },
            "effective_input_price_per_million": "8.00000000",
            "effective_output_price_per_million": "28.00000000",
            "has_discount": False,
            "supports_vision": False,
            "supports_tools": True,
            "supports_reasoning": True,
        },
        {
            "id": "qwen3.7-max",
            "owned_by": "tokenrhythm",
            "context_length": 1000000,
            "max_completion_tokens": 131072,
            "currency": "CNY",
            "pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "prompt": "12.00000000",
                "completion": "36.00000000",
            },
            "effective_input_price_per_million": "6.00000000",
            "effective_output_price_per_million": "18.00000000",
            "has_discount": True,
            "supports_vision": False,
            "supports_tools": True,
            "supports_reasoning": True,
        },
        {
            "id": "neohorse-1-9b",
            "owned_by": "tokenrhythm",
            "context_length": 262144,
            "max_completion_tokens": 32768,
            "currency": "CNY",
            "pricing": {
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "prompt": "0.60000000",
                "completion": "1.00000000",
            },
            "effective_input_price_per_million": "0.00000000",
            "effective_output_price_per_million": "0.00000000",
            "has_discount": True,
            "supports_vision": False,
            "supports_tools": True,
            "supports_reasoning": False,
        },
    ]
}


def test_zenmux_is_reachable_only_through_the_proxy() -> None:
    """Measured: the host refuses a direct connection from this machine.

    Its catalogue endpoint is public, so discovery works before any key exists, but
    chat probes need ZENMUX_API_KEY. The base path carries an unusual /api prefix.
    """
    provider = get_provider_config("zenmux")
    assert provider is not None
    assert provider.base_url == "https://zenmux.ai/api/v1"
    assert provider.models_endpoint == "/models"
    assert provider.api_key_env == "ZENMUX_API_KEY"
    assert provider.network == "proxy"
    assert provider.auth_style == "bearer"
    assert provider.free_only is True
    assert [m.id for m in get_models_for_provider("zenmux")] == []


def test_per_million_prices_are_not_rescaled(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter gives USD per token, TokenRhythm gives CNY per 1M tokens.

    Applying the per-token multiplier to the second shape would have published
    ¥8,000,000 per million tokens, so the published unit has to drive the scale.
    """
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "test-key")
    service = _service_with_payload(TOKENRHYTHM_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    by_id = {m.id: m for m in service._discovered_models if m.provider == "tokenrhythm"}
    glm = by_id["glm-5.1"]
    assert glm.pricing_input_per_1m == 8.0
    assert glm.pricing_output_per_1m == 28.0
    assert glm.pricing_currency == "CNY"
    assert glm.max_output_tokens == 128000
    assert set(glm.capabilities) == {"chat", "function_calling", "reasoning"}
    assert "long_context" not in glm.capabilities


def test_the_rate_the_account_pays_is_the_one_displayed(monkeypatch: pytest.MonkeyPatch) -> None:
    """TokenRhythm publishes base and discounted prices; the panel shows the effective one."""
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "test-key")
    service = _service_with_payload(TOKENRHYTHM_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    by_id = {m.id: m for m in service._discovered_models if m.provider == "tokenrhythm"}
    assert by_id["qwen3.7-max"].pricing_input_per_1m == 6.0
    assert by_id["qwen3.7-max"].pricing_note == "限时折扣"
    assert "long_context" in by_id["qwen3.7-max"].capabilities
    assert by_id["neohorse-1-9b"].is_free is True


def test_per_token_publishers_keep_the_old_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `pricing` object without a unit is OpenRouter's per-token USD shape; it must not drift."""
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    by_id = {m.id: m for m in service._discovered_models if m.provider == "moonshot"}
    pro = by_id["deepseek-v4-pro"]
    assert pro.pricing_input_per_1m == 20_000.0
    assert pro.pricing_currency == "USD"
    assert pro.pricing_note == ""


# Transcribed from the live https://zenmux.ai/api/v1/models on 2026-10-06 (201 models).
# ZenMux prices in USD per 1M tokens and splits each price into volume tiers; it also
# lists image, video, TTS and embedding models beside the chat ones.
ZENMUX_CATALOG: dict[str, Any] = {
    "data": [
        {
            "id": "z-ai/glm-4.7-flash-free",
            "display_name": "Z.AI: GLM 4.7 Flash (Free)",
            "owned_by": "z-ai",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "capabilities": {"reasoning": True},
            "context_length": 200000,
            "pricings": {
                "prompt": [{"value": 0, "unit": "perMTokens", "currency": "USD"}],
                "completion": [{"value": 0, "unit": "perMTokens", "currency": "USD"}],
            },
        },
        {
            "id": "inclusionai/ming-image-0.1-design",
            "display_name": "inclusionAI: Ming Image 0.1 Design",
            "owned_by": "inclusionai",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "capabilities": {"reasoning": False},
            "context_length": 8000,
            "pricings": {
                "prompt": [{"value": 0, "unit": "perMTokens", "currency": "USD"}],
                "completion": [{"value": 0, "unit": "perMTokens", "currency": "USD"}],
            },
        },
        {
            "id": "openai/gpt-6.1-sol",
            "display_name": "OpenAI: GPT-6.1 Sol",
            "owned_by": "openai",
            "input_modalities": ["text", "image", "file"],
            "output_modalities": ["text"],
            "capabilities": {"reasoning": True},
            "context_length": 1050000,
            "pricings": {
                "prompt": [
                    {"value": 2, "unit": "perMTokens", "currency": "USD"},
                    {"value": 4, "unit": "perMTokens", "currency": "USD"},
                ],
                "completion": [
                    {"value": 10, "unit": "perMTokens", "currency": "USD"},
                    {"value": 15, "unit": "perMTokens", "currency": "USD"},
                ],
            },
        },
    ]
}


def _zenmux_metadata(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setenv("ZENMUX_API_KEY", "test-key")
    checker, _ = _counting_checker(monkeypatch, payload=ZENMUX_CATALOG)
    models, error = run(checker.discover_models_detailed("zenmux"))
    assert error is None
    assert models is not None
    return models


def test_zenmux_metadata_is_parsed_from_its_own_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """ZenMux publishes several tiers per model; the entry tier is the quoted price."""
    by_id = {m["id"]: m for m in _zenmux_metadata(monkeypatch)}

    paid = by_id["openai/gpt-6.1-sol"]
    assert paid["pricing_input_per_1m"] == 2.0
    assert paid["pricing_output_per_1m"] == 10.0
    assert paid["pricing_currency"] == "USD"
    assert paid["is_free"] is False
    assert "reasoning" in paid["capabilities"]
    assert "vision" in paid["capabilities"]
    assert "long_context" in paid["capabilities"]

    free = by_id["z-ai/glm-4.7-flash-free"]
    assert free["is_free"] is True
    assert free["name"] == "Z.AI: GLM 4.7 Flash (Free)"


def test_free_only_and_non_chat_outputs_are_filtered_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 of ZenMux's 201 rows are free; three of those generate images, not text."""
    monkeypatch.setenv("ZENMUX_API_KEY", "test-key")
    service = _service_with_payload(ZENMUX_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    found = {m.id for m in service._discovered_models if m.provider == "zenmux"}
    assert found == {"z-ai/glm-4.7-flash-free"}
    # Retirement reads the listing, not the watched subset: everything the upstream
    # still serves has to count as present, paid and image rows included.
    assert service._listed["zenmux"] == {
        "z-ai/glm-4.7-flash-free",
        "inclusionai/ming-image-0.1-design",
        "openai/gpt-6.1-sol",
    }


def test_free_providers_no_longer_lose_their_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter rows used to be built with context 0 by a name-specific branch."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    catalog = {
        "data": [
            {
                "id": "free/thing",
                "name": "Free Thing",
                "context_length": 512000,
                "input_modalities": ["text", "image"],
                "pricing": {"prompt": "0", "completion": "0"},
            }
        ]
    }
    service = _service_with_payload(catalog, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    row = next(m for m in service._discovered_models if m.provider == "openrouter")
    assert row.context_length == 512000
    assert row.name == "Free Thing"
    assert "vision" in row.capabilities
    assert row.is_free is True


# ---------------------------------------------------------------- provider switch

DISABLED = {"agentrouter", "mimo", "anyrouter"}
# Static catalog rows per disabled provider; discovery is switched off for all three,
# so these counts also pin that disabling never empties the catalog.
DISABLED_MODEL_COUNTS = {"anyrouter": 11, "agentrouter": 3, "mimo": 4}
DISABLED_HOSTS = ("agentrouter.org", "token-plan-cn.xiaomimimo.com", "anyrouter.net")


def test_the_three_dead_providers_default_to_off() -> None:
    """Config carries defaults only; the settings screen owns the live switch."""
    assert {k for k, p in PROVIDERS.items() if not p.default_enabled} == DISABLED
    assert provider_default_enabled("sensenova") is True
    # An unknown key must not be probeable just because nobody declared it.
    assert provider_default_enabled("not-a-provider") is False


def test_an_unstored_switch_falls_back_to_its_default() -> None:
    """A fresh database has no rows, so defaults are what the user sees first."""
    effective = run(effective_enabled())
    assert set(effective) == set(PROVIDERS)
    assert {k for k, v in effective.items() if not v} == DISABLED
    assert run(is_enabled("sensenova")) is True
    assert run(is_enabled("not-a-provider")) is False


def test_a_stored_switch_overrides_the_default_in_both_directions() -> None:
    """Turning a dead provider back on is the whole point, so it must win over config."""
    run(database.set_provider_pref("anyrouter", True))
    run(database.set_provider_pref("moonshot", False))
    assert run(is_enabled("anyrouter")) is True
    assert run(is_enabled("moonshot")) is False
    # The default stays put: a later reset returns the provider to its shipped state.
    assert provider_default_enabled("moonshot") is True


def test_switches_survive_a_restart() -> None:
    """Persistence is what makes this a setting rather than a request-scoped flag."""
    run(database.set_provider_pref("agentrouter", False))
    prefs = run(database.get_provider_prefs())
    assert prefs == {"agentrouter": False}


def test_provider_switch_replaced_the_per_model_flags() -> None:
    """AnyRouter's 11 duplicated probe_mode rows must have collapsed into one switch."""
    assert [m.id for m in get_static_models() if m.probe_mode == "none"] == []


def test_discovery_never_contacts_a_disabled_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    contacted = {u for u in service.recorded_requests if any(h in u for h in DISABLED_HOSTS)}  # type: ignore[attr-defined]
    assert contacted == set()
    assert [m for m in service._discovered_models if m.provider in DISABLED] == []


def test_discovery_follows_the_switch_not_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider switched on from settings must be reachable by discovery."""
    async def scenario() -> tuple[set[str], list[str]]:
        service = _service_with_payload(RICH_CATALOG, monkeypatch)
        await database.set_provider_pref("mimo", True)
        await service._refresh_discovered_models(await effective_enabled())
        urls = {u for u in service.recorded_requests if "token-plan-cn.xiaomimimo.com" in u}  # type: ignore[attr-defined]
        return urls, [m.provider for m in service._discovered_models]

    urls, discovered_providers = run(scenario())
    assert urls
    assert "mimo" in discovered_providers


def test_scan_skips_every_model_of_a_disabled_provider() -> None:
    requested: list[dict[str, str]] = []
    service = _stub_service([])
    assert service._checker is not None

    async def fake_batch(
        probes: list[dict[str, str]], concurrency: int = 8
    ) -> list[ProbeResult]:
        requested.extend(probes)
        return []

    service._checker.probe_batch = fake_batch  # type: ignore[method-assign]
    run(service.run_sync())

    assert requested
    assert [p for p in requested if p["provider"] in DISABLED] == []

    rows = run(database.get_all_health())
    for key, count in DISABLED_MODEL_COUNTS.items():
        own = [r for r in rows if r["provider"] == key]
        assert len(own) == count, key
        assert {r["status"] for r in own} == {"unknown"}
        assert all("Probe disabled" in (r["error_message"] or "") for r in own)


@pytest.mark.parametrize(
    "path", ["/api/scan/agentrouter", "/api/scan/agentrouter/claude-opus-4-6"]
)
def test_scan_requests_against_a_disabled_provider_are_refused(path: str) -> None:
    response = _client(_stub_service()).post(path)
    assert response.status_code == 409
    assert "switched off" in response.json()["detail"]


def test_disabled_providers_are_hidden_from_the_dashboard() -> None:
    """The user asked to switch them off, so the panel must stop showing them."""
    payload = _client(_stub_service()).get("/api/models").json()
    assert {m["provider"] for m in payload["models"]} & DISABLED == set()
    assert {p["key"] for p in payload["providers"]} & DISABLED == set()


def test_the_settings_list_shows_every_provider_including_disabled() -> None:
    """Hiding switched-off providers here would lock the user out of switching them back.

    This is the bug the feature exists to fix, so it is pinned on the endpoint that
    serves the panel rather than on the dashboard payload.
    """
    payload = _client(_stub_service()).get("/api/providers").json()
    by_key = {p["key"]: p for p in payload["providers"]}
    assert set(by_key) == set(PROVIDERS)
    for key in DISABLED:
        assert by_key[key]["enabled"] is False
        assert by_key[key]["default_enabled"] is False
        assert by_key[key]["model_count"] == DISABLED_MODEL_COUNTS[key]


def test_toggling_a_provider_persists_and_comes_back_on_the_list() -> None:
    service = _stub_service([])

    async def no_scan() -> None:
        return None

    # The refresh itself is another test's business; here it would start real traffic.
    service.start_scan = no_scan  # type: ignore[method-assign]
    client = _client(service)

    response = client.put("/api/providers/anyrouter", {"enabled": True})
    assert response.status_code == 200
    assert {p["key"]: p["enabled"] for p in response.json()["providers"]}["anyrouter"] is True
    assert run(database.get_provider_prefs()) == {"anyrouter": True}

    # A second reader sees the stored switch, which is what a restart would do too.
    assert {p["key"]: p["enabled"] for p in client.get("/api/providers").json()["providers"]}[
        "anyrouter"
    ] is True


def test_switching_a_provider_on_starts_a_refresh() -> None:
    """Enabling must repopulate the panel, and only a full scan can discover models."""
    service = _stub_service([])
    started: list[int] = []

    async def fake_start_scan() -> None:
        started.append(1)

    service.start_scan = fake_start_scan  # type: ignore[method-assign]
    run(service.set_provider_enabled("anyrouter", True))
    assert started == [1]

    run(service.set_provider_enabled("anyrouter", False))
    assert started == [1], "switching off should not spend requests on a refresh"


def test_toggling_an_unknown_provider_is_404() -> None:
    response = _client(_stub_service()).put("/api/providers/not-a-provider", {"enabled": True})
    assert response.status_code == 404


def test_toggling_from_another_origin_is_refused() -> None:
    """The switch is a write, so it carries the same origin guard as the scan routes."""
    async def scenario() -> httpx.Response:
        app = FastAPI()
        app.include_router(router, prefix="/api")
        api.routes.sync_service = _stub_service([])
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://dashboard"
        ) as client:
            return await client.put(
                "/api/providers/anyrouter",
                json={"enabled": True},
                headers={"Origin": "http://evil.example"},
            )

    response = run(scenario())
    assert response.status_code == 403
    assert run(database.get_provider_prefs()) == {}


def test_dashboard_counts_follow_the_switch() -> None:
    """Totals describe what is monitored, so they must shrink when a provider is off."""
    client = _client(_stub_service())
    before = client.get("/api/models").json()
    client.put("/api/providers/moonshot", {"enabled": False})
    after = client.get("/api/models").json()

    assert after["total_models"] == before["total_models"] - _model_count("moonshot")
    assert {p["key"] for p in after["providers"]} & DISABLED == set()


def _model_count(provider_key: str) -> int:
    return sum(1 for m in get_static_models() if m.provider == provider_key)


# ---------------------------------------------------------------- retired models

GONE = "moonshot::moonshot-v1-8k"

# Collected from the providers this week; each words "this model is gone" its own way,
# and retirement has to recognise all of them rather than the one phrasing that happened
# to be coded first.
MODEL_MISSING_ANSWERS = [
    "Error code: 422 - {'error': {'message': 'Model Not Exist: DeepSeek-R1-0528'}}",
    "Error code: 400 - {'error': {'code': '400', 'message': 'Unsupported model mimo-v2-pro'}}",
    "Error code: 404 - {'error': {'code': 'model_not_found', 'message': 'Unknown model glm-4.7-flash'}}",
    "The model `moonshot-v1-auto` does not exist or you do not have access to it.",
    "Model not found at provider",
]

# Every one of these leaves the model's existence an open question.
ANSWERS_THAT_ARE_NOT_EVIDENCE = [
    "Error code: 429 - {'error': {'message': 'Rate limit reached'}}",
    "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}",
    "Error code: 401 - {'error': {'message': 'Invalid Authentication'}}",
    "timed out",
    "Empty response",
]


@pytest.mark.parametrize("message", MODEL_MISSING_ANSWERS)
def test_a_gone_model_is_recognised_however_it_is_worded(message: str) -> None:
    assert is_model_missing(message) is True


@pytest.mark.parametrize("message", ANSWERS_THAT_ARE_NOT_EVIDENCE)
def test_a_transport_or_quota_failure_is_not_a_gone_model(message: str) -> None:
    assert is_model_missing(message) is False


def _stub_probes(service: SyncService, statuses: dict[str, str], asked: list[str]) -> None:
    """Replace the probe fan-out with verdicts of our choosing, keyed provider::model."""
    assert service._checker is not None

    async def fake_batch(
        probes: list[dict[str, str]], concurrency: int = 8
    ) -> list[ProbeResult]:
        asked.extend(f"{p['provider']}::{p['model_id']}" for p in probes)
        return [
            ProbeResult(
                model_id=p["model_id"],
                provider=p["provider"],
                status=statuses.get(f"{p['provider']}::{p['model_id']}", "online"),
            )
            for p in probes
        ]

    service._checker.probe_batch = fake_batch  # type: ignore[method-assign]


def _rescan(service: SyncService) -> dict[str, Any]:
    """One synchronous scan, cooldown lifted so a test can scan twice in a row."""
    service._last_scan_started = 0.0
    result: dict[str, Any] = run(service.run_sync())
    return result


def test_a_rejected_model_that_is_not_listed_is_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both signals agree: the provider refuses it and its catalogue omits it."""
    service = _service_with_payload({"data": []}, monkeypatch)
    _stub_probes(service, {GONE: "offline"}, [])

    summary = _rescan(service)

    assert service._retired == {("moonshot", "moonshot-v1-8k")}
    assert summary["retired"] == 1
    stored = run(database.get_retirements())
    assert list(stored) == [("moonshot", "moonshot-v1-8k")]
    assert datetime.fromisoformat(stored[("moonshot", "moonshot-v1-8k")])


def test_a_model_still_on_the_catalogue_is_never_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint that rejects a listed model is a broken probe, not a retirement."""
    listed = {"data": [{"id": "moonshot-v1-8k"}]}
    service = _service_with_payload(listed, monkeypatch)
    _stub_probes(service, {GONE: "offline"}, [])

    _rescan(service)

    assert service._retired == set()
    assert run(database.get_retirements()) == {}


def test_a_model_that_answers_although_unlisted_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek ships a two-entry /models while still serving deepseek-chat.

    Trusting the catalogue alone would have deleted working models, so a model that
    answers a real request is kept no matter what the list says.
    """
    service = _service_with_payload({"data": []}, monkeypatch)
    _stub_probes(service, {}, [])

    _rescan(service)

    assert service._retired == set()


def test_a_failed_catalogue_lookup_is_no_evidence_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 500 from /models proves nothing about any model, so nothing may be dropped."""
    service = _service_with_payload({"data": []}, monkeypatch, status=500)
    _stub_probes(service, {GONE: "offline"}, [])

    _rescan(service)

    assert service._retired == set()


def test_retired_models_leave_the_dashboard_and_stop_costing_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_payload({"data": []}, monkeypatch)
    asked: list[str] = []
    _stub_probes(service, {GONE: "offline"}, asked)

    before = _client(service).get("/api/models").json()["total_models"]
    _rescan(service)
    asked.clear()
    _rescan(service)

    assert GONE not in asked, "a retired model must not be probed again"
    after = _client(service).get("/api/models").json()
    assert "moonshot-v1-8k" not in {m["id"] for m in after["models"]}
    assert after["total_models"] == before - 1


def test_a_retired_model_comes_back_when_the_catalogue_lists_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement is a claim about the live list, so it has to survive being wrong."""
    catalogue: dict[str, Any] = {"data": []}
    service = _service_with_payload(catalogue, monkeypatch)
    _stub_probes(service, {GONE: "offline"}, [])

    _rescan(service)
    assert service._retired == {("moonshot", "moonshot-v1-8k")}

    _stub_probes(service, {}, [])
    catalogue["data"] = [{"id": "moonshot-v1-8k"}]
    _rescan(service)

    assert service._retired == set()
    assert run(database.get_retirements()) == {}


def test_retirements_are_listed_and_restorable_through_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_payload({"data": []}, monkeypatch)
    _stub_probes(service, {GONE: "offline"}, [])
    _rescan(service)
    client = _client(service)

    listed = client.get("/api/retirements").json()["retired"]
    assert [(r["provider"], r["model_id"]) for r in listed] == [("moonshot", "moonshot-v1-8k")]
    assert listed[0]["provider_name"] == "Moonshot AI Platform"

    after = client.delete("/api/retirements/moonshot/moonshot-v1-8k").json()["retired"]
    assert after == []
    assert service._retired == set()
    payload = client.get("/api/models").json()
    assert "moonshot-v1-8k" in {m["id"] for m in payload["models"]}


def test_a_slashed_model_id_is_restorable(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter-style ids contain slashes, so the path segment has to survive them."""
    service = _service_with_payload({"data": []}, monkeypatch)
    service._retired = {("openrouter", "deepseek/deepseek-chat")}
    run(database.retire_model("openrouter", "deepseek/deepseek-chat"))
    client = _client(service)

    response = client.delete("/api/retirements/openrouter/deepseek/deepseek-chat")
    assert response.status_code == 200
    assert response.json()["retired"] == []
    assert run(database.get_retirements()) == {}


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/api/retirements/not-a-provider/kimi", 404), ("/api/retirements/moonshot/gone", 200)],
)
def test_restoring_validates_the_provider(
    monkeypatch: pytest.MonkeyPatch, path: str, expected: int
) -> None:
    service = _service_with_payload({"data": []}, monkeypatch)
    assert _client(service).delete(path).status_code == expected


def test_restoring_from_another_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retirement list is read-only, but restoring writes."""
    service = _service_with_payload({"data": []}, monkeypatch)
    _stub_probes(service, {GONE: "offline"}, [])
    _rescan(service)

    response = _client(service).delete(
        "/api/retirements/moonshot/moonshot-v1-8k", {"Origin": "http://evil.example"}
    )

    assert response.status_code == 403
    assert list(run(database.get_retirements())) == [("moonshot", "moonshot-v1-8k")]


def test_a_provider_refresh_applies_the_same_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refreshing one provider judges its own models — and nobody else's."""
    service = _service_with_payload({"data": []}, monkeypatch)
    _stub_probes(service, {GONE: "offline"}, [])
    assert service._checker is not None
    run(service._refresh_discovered_models(run(effective_enabled())))

    models = [m for m in service._get_all_models() if m.provider == "moonshot"]
    before = len(service.recorded_requests)  # type: ignore[attr-defined]
    summary = run(service._probe_provider_models("moonshot", models, service._checker))

    assert summary["retired"] == 1
    assert service._retired == {("moonshot", "moonshot-v1-8k")}
    fresh = service.recorded_requests[before:]  # type: ignore[attr-defined]
    assert not any("scnet" in u or "deepseek" in u for u in fresh), (
        "a moonshot refresh went reading other providers"
    )


def test_a_provider_scan_does_not_reprobe_a_retired_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_payload({"data": []}, monkeypatch)
    service._retired = {("moonshot", "moonshot-v1-8k")}
    seen: dict[str, list[str]] = {}

    async def fake_probe(provider_key: str, models: list[ModelConfig], checker: HealthChecker) -> dict[str, Any]:
        seen["ids"] = [m.id for m in models]
        return {"status": "success"}

    service._probe_provider_models = fake_probe  # type: ignore[method-assign]
    verdict = run(service.start_provider_scan("moonshot"))

    assert verdict is None
    assert "moonshot-v1-8k" not in seen["ids"]


def test_a_prefixed_listing_still_protects_a_live_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini answers "models/gemini-2.5-pro" while the catalogue row carries the bare id.

    Comparing the raw strings would retire a model that is simply named differently by
    the two endpoints, so the listing has to be normalised the same way discovery is.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    service = _service_with_payload({"data": [{"id": "models/gemini-2.5-pro"}]}, monkeypatch)
    _stub_probes(service, {"gemini::gemini-2.5-pro": "offline"}, [])

    _rescan(service)

    assert ("gemini", "gemini-2.5-pro") not in service._retired
    assert run(database.get_retirements()) == {}


def test_provider_settings_count_retired_models_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hidden is not the same as gone: the panel has to show what it dropped."""
    service = _service_with_payload({"data": []}, monkeypatch)
    _stub_probes(service, {GONE: "offline"}, [])
    _rescan(service)

    moonshot = {p["key"]: p for p in _client(service).get("/api/providers").json()["providers"]}[
        "moonshot"
    ]
    assert moonshot["retired_count"] == 1
    assert moonshot["model_count"] == _model_count("moonshot") - 1


def test_speech_models_are_not_discovered_into_a_chat_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MiMo ships -asr and -tts variants; neither is a chat model anyone compares here."""
    catalogue = {"data": [{"id": "mimo-v2.6-asr"}, {"id": "mimo-v2.6-tts"}, {"id": "mimo-v2.6-pro"}]}
    monkeypatch.setenv("MIMO_PAYG_API_KEY", "test-key")
    service = _service_with_payload(catalogue, monkeypatch)
    run(service._refresh_discovered_models(run(effective_enabled())))

    found = {m.id for m in service._discovered_models if m.provider == "mimo_payg"}
    assert found == {"mimo-v2.6-pro"}


# ---------------------------------------------------------------- catalogue caching


def _counting_checker(
    monkeypatch: pytest.MonkeyPatch,
    status: int = 200,
    payload: dict[str, Any] | None = None,
    delay: float = 0.0,
) -> tuple[HealthChecker, list[str]]:
    """A checker whose httpx clients record every request they make."""
    recorded: list[str] = []
    body: dict[str, Any] = payload if payload is not None else {"data": [{"id": "m0"}]}

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(str(request.url))
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(status, json=body)

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    checker = HealthChecker()
    transport = httpx.MockTransport(handler)
    checker._direct_client = httpx.AsyncClient(transport=transport)
    checker._proxy_client = httpx.AsyncClient(transport=transport)
    return checker, recorded


def test_a_scan_fetches_a_provider_catalogue_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured regression: 40 models of one provider used to raise 6 duplicate requests."""
    catalogue = {"data": [{"id": f"m{i}"} for i in range(40)]}
    checker, urls = _counting_checker(monkeypatch, payload=catalogue)
    probes = [{"model_id": f"m{i}", "provider": "openrouter"} for i in range(40)]

    results = run(checker.probe_batch(probes, concurrency=6))

    assert [u for u in urls if u.endswith("/models")] == [urls[0]]
    assert all(r.status == "online" for r in results)


def test_concurrent_lookups_join_one_inflight_request(monkeypatch: pytest.MonkeyPatch) -> None:
    checker, urls = _counting_checker(monkeypatch, delay=0.05)
    provider = PROVIDERS["openrouter"]

    async def scenario() -> list[Any]:
        return list(await asyncio.gather(*(checker._fetch_provider_models(provider) for _ in range(20))))

    snapshots = run(scenario())
    assert len(urls) == 1
    assert all(snap[0] == {"m0"} for snap in snapshots)


def test_a_failed_lookup_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures used to be re-fetched once per model, amplifying an outage 1:1 with size."""
    checker, urls = _counting_checker(monkeypatch, status=500)
    provider = PROVIDERS["openrouter"]

    async def scenario() -> list[Any]:
        return [await checker._fetch_provider_models(provider) for _ in range(10)]

    snapshots = run(scenario())
    assert len(urls) == 1
    assert all(snap[2] == "HTTP 500" for snap in snapshots)


def test_reset_model_cache_forces_a_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    checker, urls = _counting_checker(monkeypatch)
    provider = PROVIDERS["openrouter"]

    async def scenario() -> None:
        await checker._fetch_provider_models(provider)
        await checker._fetch_provider_models(provider)
        checker.reset_model_cache()
        await checker._fetch_provider_models(provider)

    run(scenario())
    assert len(urls) == 2


def test_cache_ttl_outlives_a_full_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old 30s TTL expired during the ~40s scan it was meant to cover."""
    checker, _ = _counting_checker(monkeypatch)
    assert checker._cache_ttl_ms > 60_000


# ---------------------------------------------------------------- configuration


def test_static_models_reference_known_providers() -> None:
    assert {m.provider for m in get_static_models()} <= set(PROVIDERS)


def test_provider_api_keys_are_documented() -> None:
    documented = set(re.findall(r"^([A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), re.M))
    assert documented >= PROVIDER_KEYS


def test_provider_api_keys_are_not_shared() -> None:
    env_names = [p.api_key_env for p in PROVIDERS.values()]
    assert len(env_names) == len(set(env_names))


def test_get_provider_config_round_trip() -> None:
    assert get_provider_config("deepseek") is not None
    assert get_provider_config("nope") is None


# ---------------------------------------------------------------- database


def test_health_upsert_and_read_back() -> None:
    async def scenario() -> None:
        await database.init_db()
        await database.upsert_health(
            {"model_id": "m1", "provider": "deepseek", "status": "online", "latency_ms": 42}
        )
        rows = await database.get_health_for_provider("deepseek")
        assert [(r["status"], r["latency_ms"]) for r in rows] == [("online", 42)]
        assert rows[0]["last_checked"]

    run(scenario())


def test_scan_log_lifecycle() -> None:
    async def scenario() -> None:
        await database.init_db()
        scan_id = await database.log_scan_start()
        assert scan_id is not None
        await database.log_scan_finish(scan_id, models_checked=3, models_online=2)
        assert await database.get_last_scan_time() is not None
        assert await database.get_scan_stats() == {"total_scans": 1, "total_online_ever": 2}

    run(scenario())


def test_log_scan_finish_tolerates_missing_row() -> None:
    """A failed INSERT must not turn scan finalisation into a second crash."""

    async def scenario() -> None:
        await database.init_db()
        await database.log_scan_finish(None, models_checked=0, models_online=0)
        assert await database.get_scan_stats() == {"total_scans": 0, "total_online_ever": 0}

    run(scenario())


def test_wal_and_busy_timeout_are_in_effect() -> None:
    async def scenario() -> None:
        async with database.connect() as db:
            async with db.execute("PRAGMA journal_mode") as cursor:
                mode = await cursor.fetchone()
            async with db.execute("PRAGMA busy_timeout") as cursor:
                timeout = await cursor.fetchone()
        assert mode is not None and str(mode[0]).lower() == "wal"
        assert timeout is not None and timeout[0] == 5000

    run(scenario())


def test_concurrent_writes_and_reads_do_not_deadlock() -> None:
    """The regression: a scan persisting hundreds of rows while the dashboard polls.

    Each call takes its own connection, so this genuinely exercises two or more
    connections against one WAL file rather than serialising onto one handle.
    """

    async def scenario() -> None:
        writes = [
            database.upsert_health(
                {"model_id": f"m{i}", "provider": "scnet", "status": "online", "latency_ms": i}
            )
            for i in range(120)
        ]
        reads = [database.get_all_health(), database.get_scan_stats(), database.get_last_scan_time()]
        await asyncio.gather(*writes, *reads)
        assert len(await database.get_all_health()) == 120

    run(scenario())


# ---------------------------------------------------------------- read path


def test_dashboard_triggers_no_outbound_requests() -> None:
    """GET /api/models is read-only: discovery belongs to the scheduler, not the poller."""
    service = _stub_service()
    response = _client(service).get("/api/models")
    assert response.status_code == 200
    assert service.recorded_requests == []  # type: ignore[attr-defined]
    # Switched-off providers are hidden, so the panel reports the monitored catalogue.
    hidden = sum(DISABLED_MODEL_COUNTS.values())
    assert response.json()["total_models"] == len(STATIC_MODELS) - hidden


def test_dashboard_reports_persisted_health_without_probing() -> None:
    target = STATIC_MODELS[0]

    async def seed() -> None:
        await database.upsert_health(
            {"model_id": target.id, "provider": target.provider, "status": "online", "latency_ms": 12}
        )

    run(seed())
    # The client drives its own turn on the shared loop, so it is not called from
    # inside a running coroutine.
    payload = _client(_stub_service()).get("/api/models").json()
    match = next(m for m in payload["models"] if m["id"] == target.id)
    assert match["health"]["status"] == "online"
    assert match["health"]["latency_ms"] == 12


def test_model_detail_lookup_and_missing() -> None:
    client = _client(_stub_service())
    assert client.get(f"/api/models/{STATIC_MODELS[0].id}").status_code == 200
    assert client.get("/api/models/not-a-real-model").status_code == 404


def test_routes_reject_uninitialised_service() -> None:
    assert _client(None).get("/api/models").status_code == 503


# ---------------------------------------------------------------- routing


def test_provider_scan_rejects_unknown_provider() -> None:
    assert _client(_stub_service()).post("/api/scan/bogus-provider").status_code == 404


@pytest.mark.parametrize("raw_id", ["deepseek/deepseek-chat", "deepseek%2Fdeepseek-chat"])
def test_model_scan_path_covers_slashed_ids(raw_id: str) -> None:
    """OpenRouter-style IDs embed a slash, so a fixed two-segment path used to 404 on them."""
    seen: dict[str, tuple[str, str]] = {}
    service = _stub_service()

    async def fake_probe(model_id: str, provider_key: str) -> ProbeResult:
        seen["args"] = (model_id, provider_key)
        return ProbeResult(model_id=model_id, provider=provider_key, status="online")

    service.probe_single_model = fake_probe  # type: ignore[method-assign]
    response = _client(service).post(f"/api/scan/openrouter/{raw_id}")
    assert response.status_code == 200, response.text
    model_id, provider_key = seen["args"]
    assert provider_key == "openrouter"
    assert "%2F" not in model_id
    assert model_id.split("/") == ["deepseek", "deepseek-chat"]


def test_model_scan_rejects_unknown_provider() -> None:
    assert _client(_stub_service()).post("/api/scan/bogus/some-model").status_code == 404


# ---------------------------------------------------------------- scan slot


def test_scan_slot_is_exclusive_then_rate_limited() -> None:
    async def scenario() -> None:
        service = SyncService()
        assert await service.acquire_scan() is None
        assert await service.acquire_scan() == "already_scanning"
        service.release_scan()
        assert (await service.acquire_scan()).startswith("cooldown:")  # type: ignore[union-attr]
        service.release_scan()
        service._last_scan_started = time.monotonic() - 3600
        assert await service.acquire_scan() is None

    run(scenario())


def test_concurrent_scan_triggers_yield_exactly_one_claim() -> None:
    async def scenario() -> None:
        service = SyncService()
        service._checker = None
        verdicts = await asyncio.gather(*(service.start_scan() for _ in range(10)))
        assert sum(v is None for v in verdicts) == 1  # type: ignore[arg-type]

    run(scenario())


def test_rejected_scan_reports_reason_to_caller() -> None:
    service = SyncService()
    run(service.acquire_scan())
    payload = _client(service).post("/api/scan").json()
    assert payload == {"status": "rejected", "message": "A scan is already in progress"}


# ---------------------------------------------------------------- startup log


def test_startup_report_never_prints_key_material(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "SENTINEL-SECRET-MUST-NOT-BE-LOGGED"
    for env_name in PROVIDER_KEYS:
        monkeypatch.setenv(env_name, sentinel)

    import app as app_module

    importlib.reload(app_module)
    output = capsys.readouterr().out
    assert sentinel not in output
    assert "API keys configured" in output


def test_health_endpoint_shape() -> None:
    import app as app_module

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app_module.app), base_url="http://dashboard"
        ) as client:
            return (await client.get("/health")).json()

    payload = run(fetch())
    assert payload["status"] == "healthy"
    assert payload["uptime_seconds"] >= 0


# ---------------------------------------------------------------- network routing


class OneShotServer:
    """Answers one HTTP request at a time and records the request line it received.

    Seeing an absolute-form request line ("GET http://host/...") is what proves a
    request travelled via an HTTP proxy rather than straight to the origin.
    """

    def __init__(self) -> None:
        self.request_lines: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    data = conn.recv(4096)
                except OSError:
                    continue
                if not data:
                    continue
                self.request_lines.append(data.decode(errors="replace").split("\r\n")[0])
                try:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
        self._sock.close()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    @property
    def hits(self) -> int:
        return len(self.request_lines)


@pytest.fixture
def servers():
    origin, proxy = OneShotServer(), OneShotServer()
    yield origin, proxy
    origin.stop()
    proxy.stop()


def _get(url: str, client: httpx.AsyncClient) -> int:
    async def scenario() -> int:
        response = await client.get(url, timeout=5.0)
        return response.status_code

    return run(scenario())


def test_direct_pool_ignores_ambient_proxy(
    servers: tuple[OneShotServer, OneShotServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """trust_env=False is load-bearing: an ambient *_proxy must not capture domestic providers."""
    origin, proxy = servers
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, proxy.url)

    checker = HealthChecker()
    assert _get(origin.url, checker._direct_client) == 200
    assert proxy.hits == 0
    assert origin.request_lines[0].startswith("GET /"), origin.request_lines


def test_proxied_pool_routes_through_configured_proxy(
    servers: tuple[OneShotServer, OneShotServer],
) -> None:
    origin, proxy = servers
    checker = HealthChecker(proxy=proxy.url)
    assert _get(origin.url, checker._proxy_client) == 200
    assert proxy.hits == 1
    assert proxy.request_lines[0] == f"GET {origin.url}/ HTTP/1.1"


def test_proxied_pool_without_proxy_reaches_origin_directly(
    servers: tuple[OneShotServer, OneShotServer],
) -> None:
    origin, proxy = servers
    checker = HealthChecker(proxy=None)
    assert _get(origin.url, checker._proxy_client) == 200
    assert proxy.hits == 0


def test_proxy_url_prefers_explicit_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as app_module

    for name in ("MODELSCOUT_PROXY_URL", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("MODELSCOUT_PROXY_URL", "http://127.0.0.1:8888")
    assert app_module._resolve_proxy_url() == "http://127.0.0.1:8888"

    monkeypatch.delenv("MODELSCOUT_PROXY_URL")
    assert app_module._resolve_proxy_url() == "http://127.0.0.1:7897"

    monkeypatch.delenv("https_proxy")
    assert app_module._resolve_proxy_url() is None


def test_proxy_label_never_leaks_credentials() -> None:
    import app as app_module

    assert app_module._proxy_label("http://user:secretpw@127.0.0.1:7897") == "http://127.0.0.1:7897"


# ---------------------------------------------------------------- key handling


def test_discovery_without_key_sends_no_request(
    monkeypatch: pytest.MonkeyPatch, isolated_db: str
) -> None:
    """A missing key must short-circuit, not emit `Authorization: Bearer ` and fail obscurely."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    urls: list[str] = []
    service = _stub_service(urls)
    assert service._checker is not None

    ids, error = run(service._checker.discover_models("openrouter"))
    assert ids is None
    assert "no API key" in error  # type: ignore[operator]
    assert urls == []


def test_probe_reports_no_key_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    urls: list[str] = []
    service = _stub_service(urls)
    assert service._checker is not None

    result = run(service._checker.probe("deepseek-chat", "deepseek"))
    assert result.status == "no_key"
    assert urls == []


# ---------------------------------------------------------------- probe scoping




# ---------------------------------------------------------------- access control


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.com",
        "null",  # sandboxed iframe / file:// document
        "http://localhost:3000.evil.com",  # prefix spoof
        "https://localhost:3000",  # wrong scheme
    ],
)
def test_cross_origin_scan_requests_are_rejected(origin: str) -> None:
    service = _stub_service()
    response = _client(service).post("/api/scan", headers={"Origin": origin})
    assert response.status_code == 403
    # Rejection must happen before the handler, so no paid work is kicked off.
    assert service.recorded_requests == []  # type: ignore[attr-defined]
    assert not service.is_scanning


@pytest.mark.parametrize("origin", ALLOWED_ORIGINS)
def test_dashboard_origins_may_trigger_scans(origin: str) -> None:
    response = _client(_stub_service()).post("/api/scan/deepseek", headers={"Origin": origin})
    assert response.status_code == 200


def test_request_without_origin_is_treated_as_local() -> None:
    """curl and local scripts send no Origin; they already hold the keys in .env."""
    assert is_trusted_origin(None)
    assert _client(_stub_service()).post("/api/scan").status_code == 200


def test_reads_are_not_restricted_by_origin() -> None:
    response = _client(_stub_service()).get("/api/models", headers={"Origin": "http://evil.com"})
    assert response.status_code == 200


def test_cors_and_scan_guard_share_one_allowlist() -> None:
    """The two lists must not drift, or CORS would admit an origin the guard rejects."""
    from starlette.middleware.cors import CORSMiddleware

    import app as app_module

    cors = next(m for m in app_module.app.user_middleware if m.cls is CORSMiddleware)
    configured = cors.kwargs["allow_origins"]
    assert isinstance(configured, list | tuple), configured
    assert set(map(str, configured)) == set(ALLOWED_ORIGINS)


# ---------------------------------------------------------------- probe rate limit


def test_per_model_probes_are_capped() -> None:
    probed: list[str] = []
    service = _stub_service()

    async def fake_probe(model_id: str, provider_key: str) -> ProbeResult:
        probed.append(model_id)
        return ProbeResult(model_id=model_id, provider=provider_key, status="online")

    service.probe_single_model = fake_probe  # type: ignore[method-assign]
    client = _client(service)

    codes = [client.post(f"/api/scan/deepseek/m{i}").status_code for i in range(PROBE_MAX_CALLS + 2)]
    assert codes[:PROBE_MAX_CALLS] == [200] * PROBE_MAX_CALLS
    assert codes[PROBE_MAX_CALLS:] == [429, 429]
    # The cap must stop the work, not merely the response.
    assert len(probed) == PROBE_MAX_CALLS


def test_rate_limit_advertises_retry_after() -> None:
    async def fake_probe(model_id: str, provider_key: str) -> ProbeResult:
        return ProbeResult(model_id=model_id, provider=provider_key, status="online")

    service = _stub_service()
    service.probe_single_model = fake_probe  # type: ignore[method-assign]
    client = _client(service)

    for i in range(PROBE_MAX_CALLS):
        client.post(f"/api/scan/deepseek/m{i}")
    response = client.post("/api/scan/deepseek/over-limit")
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_sliding_window_frees_slots_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("core.access.time.monotonic", lambda: clock[0])

    limiter = SlidingWindowLimiter(2, 10.0)
    assert limiter.acquire() is None
    assert limiter.acquire() is None
    assert limiter.acquire() is not None

    clock[0] += 11.0
    assert limiter.acquire() is None



# ---------------------------------------------------------------- fx rate

ER_API_HIT = (200, {"rates": {"CNY": 6.714383}})
FRANKFURT_HIT = (200, {"rates": {"CNY": 6.7046}, "date": "2026-10-02"})


def _fx_with(responses: list[tuple[int, dict[str, Any]]]) -> FxRate:
    """Queue canned responses for the sources in their declared order."""
    remaining = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        status, body = remaining.pop(0)
        return httpx.Response(status, json=body)

    return FxRate(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _refresh(fx: FxRate) -> dict[str, Any]:
    async def scenario() -> dict[str, Any]:
        try:
            return await fx.refresh()
        finally:
            await fx.aclose()

    return run(scenario())


def test_fx_reads_the_first_source() -> None:
    fx = _fx_with([ER_API_HIT])
    outcome = _refresh(fx)
    assert outcome["status"] == "updated"
    assert fx.cny_per_usd == 6.714383
    assert fx.source == "open.er-api.com"
    assert fx.fetched_at


def test_fx_falls_through_to_the_second_source() -> None:
    fx = _fx_with([(429, {}), FRANKFURT_HIT])
    outcome = _refresh(fx)
    assert outcome["status"] == "updated"
    assert fx.cny_per_usd == 6.7046
    assert fx.source == "api.frankfurter.dev"


def test_fx_keeps_the_last_good_rate_when_every_source_fails() -> None:
    fx = _fx_with([ER_API_HIT, (500, {}), (500, {})])

    async def scenario() -> dict[str, Any]:
        await fx.refresh()
        try:
            return await fx.refresh()
        finally:
            await fx.aclose()

    outcome = run(scenario())
    assert outcome["status"] == "fallback"
    # The previous good rate, not the bundled constant.
    assert fx.cny_per_usd == 6.714383
    assert len(outcome["failures"]) == 2


def test_fx_starts_at_the_fallback_when_nothing_works() -> None:
    fx = _fx_with([(500, {}), (503, {})])
    outcome = _refresh(fx)
    assert outcome["status"] == "fallback"
    assert fx.cny_per_usd == FALLBACK_CNY_PER_USD
    assert fx.source is None


@pytest.mark.parametrize("bad", [0.0, 999.0, -6.7])
def test_fx_rejects_out_of_range_rates(bad: float) -> None:
    """A malformed or unrelated payload must not silently bias every converted price."""
    fx = _fx_with([(200, {"rates": {"CNY": bad}}), (500, {})])
    outcome = _refresh(fx)
    assert outcome["status"] == "fallback"
    assert fx.cny_per_usd == FALLBACK_CNY_PER_USD


def test_fx_survives_malformed_payloads() -> None:
    fx = _fx_with([(200, {"nope": 1}), (200, {"rates": {"CNY": "abc"}})])
    outcome = _refresh(fx)
    assert outcome["status"] == "fallback"
    assert fx.cny_per_usd == FALLBACK_CNY_PER_USD


def test_fx_sources_are_two_independent_hosts() -> None:
    assert len(SOURCES) >= 2
    assert len({urlparse(u).netloc for u in SOURCES}) == len(SOURCES)


def test_dashboard_payload_carries_the_live_rate() -> None:
    fx = _fx_with([ER_API_HIT])

    async def prime() -> None:
        await fx.refresh()
        await fx.aclose()

    run(prime())
    # The route only reads the cached attribute; the client is never driven from a
    # second event loop, which is what deadlocks httpx on Linux.
    api.routes.fx_rate = fx
    payload = _client(_stub_service()).get("/api/models").json()
    assert payload["cny_per_usd"] == 6.714383


def test_dashboard_payload_falls_back_without_a_wired_rate() -> None:
    payload = _client(_stub_service()).get("/api/models").json()
    assert payload["cny_per_usd"] == FALLBACK_CNY_PER_USD


def test_read_path_still_makes_no_outbound_request_with_fx() -> None:
    """The FX design must not re-open the hole that GET /api/models used to have."""
    urls: list[str] = []
    fx = _fx_with([ER_API_HIT])

    async def prime() -> None:
        await fx.refresh()
        await fx.aclose()

    run(prime())
    api.routes.fx_rate = fx
    assert _client(_stub_service(urls)).get("/api/models").status_code == 200
    assert urls == []
