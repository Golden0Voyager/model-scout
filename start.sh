#!/bin/bash

# ModelScout v2.0 Launch Script
# -----------------------------

# Get script directory (works regardless of cwd)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Proxy: providers marked network="proxy" in backend/core/config.py route through it.
# MODELSCOUT_PROXY_URL wins; the app otherwise falls back to https_proxy/http_proxy.
# Uncomment and point at your local listener if overseas providers show as unreachable:
# export MODELSCOUT_PROXY_URL=http://127.0.0.1:7897

# Load user env (API keys, and proxy settings if you keep them in your shell config)
if [ -f ~/.zshrc ]; then
    source ~/.zshrc >/dev/null 2>&1
fi

# The dashboard talks to the local backend directly — never through the proxy.
export no_proxy="${no_proxy:+$no_proxy,}localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"

export PATH="/Users/hainingyu/.local/bin:$PATH"

# Kill existing processes on ports 8000 and 3000
echo "🧹 Cleaning up old processes..."
lsof -ti:8000 | xargs kill -9 2>/dev/null
lsof -ti:3000 | xargs kill -9 2>/dev/null
sleep 1

# 1. Start Backend
echo "🚀 Starting ModelScout Backend..."
cd "$SCRIPT_DIR/backend"
# PYTHONUNBUFFERED so backend.log updates live instead of on block-buffer flush.
PYTHONPATH=. PYTHONUNBUFFERED=1 uv run --frozen python app.py > "$SCRIPT_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo "✓ Backend started on port 8000 (PID: $BACKEND_PID)"

# 2. Start Frontend
echo "🌐 Starting ModelScout Frontend..."
cd "$SCRIPT_DIR/frontend"
export PATH=$PATH:/opt/homebrew/bin
npm run dev > "$SCRIPT_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "✓ Frontend started on port 3000 (PID: $FRONTEND_PID)"

echo ""
echo "✨ ModelScout v2.0 is now running!"
echo "👉 Dashboard: http://localhost:3000"
echo "👉 API:      http://localhost:8000/api/models"
echo "👉 Health:   http://localhost:8000/health"
echo ""
echo "Logs: backend.log / frontend.log"
echo "Stop:  kill $BACKEND_PID $FRONTEND_PID"
