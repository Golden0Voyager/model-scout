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
    get_models_for_provider,
    get_provider_config,
    get_static_models,
    provider_enabled,
)
from services.fx import SOURCES, FxRate
from services.health_checker import HealthChecker, ProbeResult
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

    def _call(self, method: str, path: str, headers: dict[str, str] | None):
        async def request() -> httpx.Response:
            async with httpx.AsyncClient(transport=self._transport, base_url="http://dashboard") as client:
                return await client.request(method, path, headers=headers or {})

        return run(request())

    def get(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return self._call("GET", path, headers)

    def post(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return self._call("POST", path, headers)


def _client(service: SyncService | None) -> Api:
    return Api(service)


def _service_with_payload(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
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
        return httpx.Response(200, json=payload)

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


def test_rich_discovery_drives_metadata_not_provider_names() -> None:
    """The rich path must be selected by config, not by an `if provider_key == ...` arm."""
    import inspect

    import services.sync_service as module

    source = inspect.getsource(module.SyncService._refresh_discovered_models)
    assert 'provider_key == "moonshot"' not in source
    for key in ("moonshot", "sensenova"):
        provider = PROVIDERS[key]
        assert provider.rich_discovery is True


# ---------------------------------------------------------------- discovery mapping


def test_rich_catalog_populates_every_advertised_field(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models())

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
    run(service._refresh_discovered_models())
    caps = {c for m in service._discovered_models for c in m.capabilities}
    assert "json_mode" not in caps
    assert not any("_" in c and c not in {"function_calling", "long_context"} for c in caps)


def test_moonshot_still_discovers_through_the_generalised_path(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models())
    moonshot = [m for m in service._discovered_models if m.provider == "moonshot"]
    assert {m.id for m in moonshot} == {"sensenova-6.8-flash-lite", "deepseek-v4-pro"}


# ---------------------------------------------------------------- provider switch

DISABLED = {"agentrouter", "mimo", "anyrouter"}
# Static catalog rows per disabled provider; discovery is switched off for all three,
# so these counts also pin that disabling never empties the catalog.
DISABLED_MODEL_COUNTS = {"anyrouter": 11, "agentrouter": 3, "mimo": 4}
DISABLED_HOSTS = ("agentrouter.org", "token-plan-cn.xiaomimimo.com", "anyrouter.net")


def test_the_three_dead_providers_are_switched_off() -> None:
    assert {k for k, p in PROVIDERS.items() if not p.enabled} == DISABLED
    assert provider_enabled("sensenova") is True
    # An unknown key must not be probeable just because nobody declared it.
    assert provider_enabled("not-a-provider") is False


def test_provider_switch_replaced_the_per_model_flags() -> None:
    """AnyRouter's 11 duplicated probe_mode rows must have collapsed into one switch."""
    assert [m.id for m in get_static_models() if m.probe_mode == "none"] == []


def test_discovery_never_contacts_a_disabled_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_payload(RICH_CATALOG, monkeypatch)
    run(service._refresh_discovered_models())

    contacted = {u for u in service.recorded_requests if any(h in u for h in DISABLED_HOSTS)}  # type: ignore[attr-defined]
    assert contacted == set()
    assert [m for m in service._discovered_models if m.provider in DISABLED] == []


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
    assert "disabled" in response.json()["detail"]


def test_disabled_providers_stay_in_the_catalog() -> None:
    """The panel reports which models exist; a dead key is not a reason to forget them."""
    payload = _client(_stub_service()).get("/api/models").json()
    assert {m["provider"] for m in payload["models"]} >= DISABLED


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
    assert response.json()["total_models"] == len(STATIC_MODELS)


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
