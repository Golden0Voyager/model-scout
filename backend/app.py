"""ModelScout API v2.0

Lightweight model monitoring dashboard.
- SQLite persistence for health history
- Config-driven model catalog
- Lightweight probes (models endpoint + minimal chat ping)
- Background scheduled scans
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import api.routes
from api.routes import router
from core.config import PROVIDERS
from services.sync_service import SyncService

load_dotenv()


def _proxy_label(url: str) -> str:
    """Return scheme://host:port, dropping any credentials embedded in the URL."""
    parsed = urlsplit(url)
    host = parsed.hostname or "?"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme or 'http'}://{host}"


def _resolve_proxy_url() -> str | None:
    """Proxy used for providers marked network="proxy".

    MODELSCOUT_PROXY_URL wins; the ambient *_proxy names are only a fallback, and
    reading them here is safe because HealthChecker builds every client with
    trust_env=False, so httpx cannot apply them to the direct pool as well.
    """
    explicit = os.getenv("MODELSCOUT_PROXY_URL", "").strip()
    if explicit:
        return explicit
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


# Report key presence only — never print key material, this output is logged to backend.log.
_KEY_ENVS = sorted({p.api_key_env for p in PROVIDERS.values()})
_missing_keys = [k for k in _KEY_ENVS if not os.getenv(k, "").strip()]
print(f"[env] API keys configured: {len(_KEY_ENVS) - len(_missing_keys)}/{len(_KEY_ENVS)}")
if _missing_keys:
    print(f"[env] missing: {', '.join(_missing_keys)}")

DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

# Global state
_start_time = time.time()
_scan_task: asyncio.Task | None = None


async def _scheduled_scan_loop(service: SyncService):
    """Background task that runs scans periodically."""
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)
            if not service.is_scanning:
                await service.run_sync()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[scheduler] Scan error: {e}")
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scan_task

    proxy_url = _resolve_proxy_url()
    proxied = [p.name for p in PROVIDERS.values() if p.network == "proxy"]
    if proxy_url:
        print(f"[net] proxy enabled for {len(proxied)} providers: {_proxy_label(proxy_url)}")
    else:
        print(f"[net] no proxy configured; marked unreachable: {', '.join(proxied)}")

    service = SyncService(proxy=proxy_url)
    await service.initialize()

    # Wire routes
    api.routes.sync_service = service

    # Initial scan on startup
    verdict = await service.start_scan()
    if verdict is not None:
        print(f"[scheduler] Startup scan skipped: {verdict}")

    # Start scheduler
    _scan_task = asyncio.create_task(_scheduled_scan_loop(service))

    print(f"🚀 ModelScout v2.0 started (scan interval: {SCAN_INTERVAL_MINUTES}min)")

    yield

    # Shutdown
    if _scan_task:
        _scan_task.cancel()
        try:
            await _scan_task
        except asyncio.CancelledError:
            pass

    await service.shutdown()
    print("👋 ModelScout shutdown complete")


app = FastAPI(
    title="ModelScout API",
    version="2.0.0",
    lifespan=lifespan,
)

if DEBUG:
    @app.middleware("http")
    async def log_requests(request, call_next):
        print(f"[DEBUG] {request.method} {request.url.path}")
        return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    from core.models import HealthResponse
    return HealthResponse(
        status="healthy",
        version="2.0.0",
        uptime_seconds=time.time() - _start_time,
    )

app.include_router(router, prefix="/api")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
