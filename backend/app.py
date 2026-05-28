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
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

import api.routes
from api.routes import router
from services.sync_service import SyncService

load_dotenv()

# Clear proxy env vars — user is abroad, direct access to all providers
for _p in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_p, None)

# Verify key loading (prefixes only, for debugging)
for _env_key in ["GROQ_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "SENSENOVA_API_KEY"]:
    _val = os.getenv(_env_key, "NOT_SET")
    _prefix = _val[:10] if len(_val) > 10 else _val
    print(f"[env] {_env_key}: {_prefix}...")

DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

# Global state
_start_time = time.time()
_scan_task: Optional[asyncio.Task] = None


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

    proxy = os.getenv("https_proxy") or os.getenv("http_proxy")
    service = SyncService(proxy=proxy)
    await service.initialize()

    # Wire routes
    api.routes.sync_service = service

    # Initial scan on startup
    asyncio.create_task(service.run_sync())

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
