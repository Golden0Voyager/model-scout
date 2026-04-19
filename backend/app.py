from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import httpx
from typing import List, Dict
from datetime import datetime
import os
import time
import sys
from dotenv import load_dotenv

# Add parent directory to sys.path to support 'backend' package imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fetchers import openrouter, groq, dashscope, siliconflow, zhipu, aihubmix, scnet
from engine.benchmarker import Benchmarker

load_dotenv()

# Global State
models_db: List[Dict] = []
last_scan_time = None
is_scanning = False
cache_expiry = None

# Shared resources (initialized in lifespan)
shared_http_client: httpx.AsyncClient = None      # 带代理，海外 API
shared_http_client_cn: httpx.AsyncClient = None    # 直连，国内 API
benchmarker: Benchmarker = None

BENCH_PER_PROVIDER = int(os.getenv("BENCH_PER_PROVIDER", "4"))
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global shared_http_client, shared_http_client_cn, benchmarker

    proxy = os.getenv("https_proxy") or os.getenv("http_proxy")
    shared_http_client = httpx.AsyncClient(
        proxy=proxy, timeout=10.0, follow_redirects=True
    )
    shared_http_client_cn = httpx.AsyncClient(
        timeout=10.0, follow_redirects=True
    )
    benchmarker = Benchmarker(proxy=proxy)

    # 启动时自动扫描
    asyncio.create_task(run_global_scan())

    yield

    # 清理资源
    await shared_http_client.aclose()
    await shared_http_client_cn.aclose()
    await benchmarker.close()


app = FastAPI(title="ModelScout API", lifespan=lifespan)

if DEBUG:
    @app.middleware("http")
    async def log_requests(request, call_next):
        print(f"[DEBUG] {request.method} {request.url.path}")
        response = await call_next(request)
        return response

# Configure CORS for Next.js
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


async def benchmark_model(model: Dict, semaphore: asyncio.Semaphore):
    """Run benchmark for a single model with concurrency control."""
    async with semaphore:
        perf = await benchmarker.run_test(model["id"], model["provider"])
        model["performance"] = perf
        # Small delay to prevent burst limits on some APIs
        await asyncio.sleep(0.3)


async def run_global_scan():
    global is_scanning, models_db, last_scan_time, cache_expiry
    if is_scanning:
        return

    is_scanning = True
    print("🚀 Starting high-throughput parallel scout...")
    provider_models = {}

    fetch_tasks = {
        # 海外平台 — 走代理
        "OpenRouter": openrouter.fetch_free_models(shared_http_client),
        "Groq": groq.fetch_free_models(shared_http_client),
        "AIHubMix": aihubmix.fetch_free_models(shared_http_client),
        # 国内平台 — 直连
        "DashScope": dashscope.fetch_free_models(shared_http_client_cn),
        "SiliconFlow": siliconflow.fetch_free_models(shared_http_client_cn),
        "ZhipuAI": zhipu.fetch_free_models(shared_http_client_cn),
        "SCNET": scnet.fetch_free_models(shared_http_client_cn),
    }

    # execute fetchers in parallel
    results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)

    all_models = []
    for provider, res in zip(fetch_tasks.keys(), results):
        if isinstance(res, Exception):
            print(f"❌ {provider} Error: {res}")
            provider_models[provider] = []
        else:
            print(f"✅ {provider}: discovered {len(res)} models")
            provider_models[provider] = res
            all_models.extend(res)

    # 一次性更新全局模型列表
    models_db = all_models
    last_scan_time = datetime.now().isoformat()
    cache_expiry = time.time() + 600

    # Parallel Benchmarking: pick top N models from each provider
    bench_batch = []
    for provider, models in provider_models.items():
        bench_batch.extend(models[:BENCH_PER_PROVIDER])

    print(f"⚡️ Concurrent benchmarking batch: {len(bench_batch)} nodes from all providers...")
    semaphore = asyncio.Semaphore(5)
    bench_tasks = [benchmark_model(m, semaphore) for m in bench_batch]

    if bench_tasks:
        await asyncio.gather(*bench_tasks)

    is_scanning = False
    print("✨ Optimization scan complete.")


@app.get("/models")
async def get_models():
    global cache_expiry
    now = time.time()
    if not is_scanning and (cache_expiry is None or now > cache_expiry):
        asyncio.create_task(run_global_scan())

    return {
        "models": models_db,
        "is_scanning": is_scanning,
        "last_scan_time": last_scan_time
    }


@app.post("/trigger_scan")
async def trigger_scan():
    if not is_scanning:
        asyncio.create_task(run_global_scan())
        return {"status": "scan_started"}
    return {"status": "already_scanning"}


@app.get("/health")
async def health():
    return {"status": "alive"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
