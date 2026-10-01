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

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
from core.config import PROVIDERS, STATIC_MODELS, get_provider_config, get_static_models
from services.health_checker import HealthChecker, ProbeResult
from services.sync_service import SyncService

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
PROVIDER_KEYS = {p.api_key_env for p in PROVIDERS.values()}


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test.db")
    asyncio.run(database.init_db())
    yield database.DB_PATH
    database.DB_PATH = original


@pytest.fixture(autouse=True)
def fresh_probe_budget():
    """The limiter is process-global, so tests must not inherit each other's budget."""
    probe_limiter.reset()
    yield
    probe_limiter.reset()


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


def _client(service: SyncService | None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    api.routes.sync_service = service
    return TestClient(app)


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

    asyncio.run(scenario())


def test_scan_log_lifecycle() -> None:
    async def scenario() -> None:
        await database.init_db()
        scan_id = await database.log_scan_start()
        assert scan_id is not None
        await database.log_scan_finish(scan_id, models_checked=3, models_online=2)
        assert await database.get_last_scan_time() is not None
        assert await database.get_scan_stats() == {"total_scans": 1, "total_online_ever": 2}

    asyncio.run(scenario())


def test_log_scan_finish_tolerates_missing_row() -> None:
    """A failed INSERT must not turn scan finalisation into a second crash."""

    async def scenario() -> None:
        await database.init_db()
        await database.log_scan_finish(None, models_checked=0, models_online=0)
        assert await database.get_scan_stats() == {"total_scans": 0, "total_online_ever": 0}

    asyncio.run(scenario())


# ---------------------------------------------------------------- read path


def test_dashboard_triggers_no_outbound_requests() -> None:
    """GET /api/models is read-only: discovery belongs to the scheduler, not the poller."""
    service = _stub_service()
    response = _client(service).get("/api/models")
    assert response.status_code == 200
    assert service.recorded_requests == []  # type: ignore[attr-defined]
    assert response.json()["total_models"] == len(STATIC_MODELS)


def test_dashboard_reports_persisted_health_without_probing() -> None:
    async def scenario() -> None:
        await database.init_db()
        target = STATIC_MODELS[0]
        await database.upsert_health(
            {"model_id": target.id, "provider": target.provider, "status": "online", "latency_ms": 12}
        )
        payload = _client(_stub_service()).get("/api/models").json()
        match = next(m for m in payload["models"] if m["id"] == target.id)
        assert match["health"]["status"] == "online"
        assert match["health"]["latency_ms"] == 12

    asyncio.run(scenario())


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

    asyncio.run(scenario())


def test_concurrent_scan_triggers_yield_exactly_one_claim() -> None:
    async def scenario() -> None:
        service = SyncService()
        service._checker = None
        verdicts = await asyncio.gather(*(service.start_scan() for _ in range(10)))
        assert sum(v is None for v in verdicts) == 1  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_rejected_scan_reports_reason_to_caller() -> None:
    async def scenario() -> None:
        service = SyncService()
        await service.acquire_scan()
        payload = _client(service).post("/api/scan").json()
        assert payload == {"status": "rejected", "message": "A scan is already in progress"}

    asyncio.run(scenario())


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

    payload: dict[str, Any] = TestClient(app_module.app).get("/health").json()
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

    return asyncio.run(scenario())


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

    ids, error = asyncio.run(service._checker.discover_models("openrouter"))
    assert ids is None
    assert "no API key" in error  # type: ignore[operator]
    assert urls == []


def test_probe_reports_no_key_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    urls: list[str] = []
    service = _stub_service(urls)
    assert service._checker is not None

    result = asyncio.run(service._checker.probe("deepseek-chat", "deepseek"))
    assert result.status == "no_key"
    assert urls == []


# ---------------------------------------------------------------- probe scoping


def test_scan_omits_models_with_probe_disabled() -> None:
    """AnyRouter's upstream is down, so its catalog rows must never reach the probe batch."""
    requested: list[dict[str, str]] = []
    service = _stub_service([])
    assert service._checker is not None

    async def fake_batch(
        probes: list[dict[str, str]], concurrency: int = 8
    ) -> list[ProbeResult]:
        requested.extend(probes)
        return []

    service._checker.probe_batch = fake_batch  # type: ignore[method-assign]
    asyncio.run(service.run_sync())

    assert requested, "expected the scan to probe the enabled models"
    assert [p for p in requested if p["provider"] == "anyrouter"] == []

    rows = asyncio.run(database.get_all_health())
    anyrouter = [r for r in rows if r["provider"] == "anyrouter"]
    assert len(anyrouter) == 11
    assert {r["status"] for r in anyrouter} == {"unknown"}
    assert all("Probe disabled" in (r["error_message"] or "") for r in anyrouter)


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

