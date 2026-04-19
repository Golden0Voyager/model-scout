#!/bin/bash

# ModelScout Launch Script
# ------------------------

# Ensure proxies are set for international API access
export http_proxy=http://127.0.0.1:7897
export https_proxy=http://127.0.0.1:7897
export all_proxy=socks5://127.0.0.1:7897
export no_proxy=localhost,127.0.0.1,::1

# Ensure environment variables from ~/.zshrc are loaded
if [ -f ~/.zshrc ]; then
    echo "Sourcing ~/.zshrc..."
    source ~/.zshrc
fi

# Set explicit PATH for uv and other tools
export PATH="/Users/hainingyu/.local/bin:$PATH"

# 1. Start Backend
echo "🚀 Starting ModelScout Backend..."
cd backend
PYTHONPATH=.. uv run python app.py > ../backend.log 2>&1 &
BACKEND_PID=$!
echo "✓ Backend started on port 8000 (PID: $BACKEND_PID)"

# 2. Start Frontend
echo "🌐 Starting ModelScout Frontend..."
cd ../frontend
export PATH=$PATH:/opt/homebrew/bin
npm run dev > ../frontend.log 2>&1 &
FRONTEND_PID=$!
echo "✓ Frontend started on port 3000 (PID: $FRONTEND_PID)"

echo ""
echo "✨ ModelScout is now running!"
echo "👉 View Dashboard: http://localhost:3000"
echo "👉 API Endpoint: http://localhost:8000/models"
echo ""
echo "To stop the services, run: kill $BACKEND_PID $FRONTEND_PID"

# Keep script alive is not needed as they run in background
