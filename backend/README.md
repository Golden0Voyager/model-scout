# ModelScout Backend v2.0

## Architecture

```
backend/
├── app.py                  # FastAPI entry point, lifespan, scheduler
├── api/
│   └── routes.py           # /models, /scan, /health
├── core/
│   ├── config.py           # Provider configs + static model catalog
│   ├── models.py           # Pydantic schemas
│   └── database.py         # SQLite persistence (aiosqlite)
└── services/
    ├── health_checker.py   # Lightweight probes (models endpoint + chat ping)
    └── sync_service.py     # Orchestrates scans and dashboard data
```

## Data Flow

1. **Static Catalog** (`core/config.py`): All models from user docs are pre-configured with metadata, pricing, capabilities.
2. **Health Probe** (`services/health_checker.py`):
   - First tries the provider's `/models` endpoint (free, fast, no token cost).
   - Falls back to a `max_tokens=1` chat completion for actual inference verification.
   - Models with `probe_mode="none"` (e.g., AnyRouter without API endpoint) are skipped.
3. **Persistence** (`core/database.py`): Health results are stored in SQLite (`model_scout.db`).
4. **Scheduler** (`app.py`): Background scan every 5 minutes + manual trigger via API.
5. **Dashboard API** (`api/routes.py`): Merges static catalog + latest health rows into a single JSON response.

## Environment Variables

Copy `.env.example` to `.env` and fill in your API keys. Missing keys result in `no_key` status — the model is still displayed, just not probed.

## Run

```bash
cd backend
PYTHONPATH=. uv run python app.py
```

Or use the root `start.sh` to launch both backend and frontend.
