#!/bin/bash
set -e

echo "=== ModelScout v2.0 Verification ==="
echo "Date: $(date)"

# 1. Backend dependencies
echo ""
echo "[1/5] Backend dependencies..."
cd backend
if [ -f .venv/bin/python3 ]; then
    echo "✅ Python venv exists"
else
    echo "⚠️  Python venv missing — run: uv sync"
    exit 1
fi
cd ..

# 2. Frontend dependencies
echo ""
echo "[2/5] Frontend dependencies..."
cd frontend
if [ -d node_modules ]; then
    echo "✅ node_modules exists"
else
    echo "⚠️  node_modules missing — run: npm install"
    exit 1
fi
cd ..

# 3. Backend health (if running)
echo ""
echo "[3/5] Backend health check..."
if curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; then
    STATUS=$(curl -s http://127.0.0.1:8000/health | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unknown")
    if [ "$STATUS" = "healthy" ]; then
        echo "✅ Backend healthy"
        MODELS=$(curl -s http://127.0.0.1:8000/api/models | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d[\"total_models\"]} models, {d[\"online_models\"]} online')" 2>/dev/null || echo "N/A")
        echo "   Models: $MODELS"
    else
        echo "⚠️  Backend responded but status='$STATUS'"
    fi
else
    echo "⚠️  Backend not running on port 8000"
fi

# 4. Frontend build
echo ""
echo "[4/5] Frontend build..."
cd frontend
if npm run build > ../frontend_build.log 2>&1; then
    echo "✅ Frontend build succeeded"
else
    echo "❌ Frontend build FAILED"
    tail -20 ../frontend_build.log
    exit 1
fi
cd ..

# 5. Logs check
echo ""
echo "[5/5] Recent errors..."
if [ -f backend.log ]; then
    ERRORS=$(grep -ci "error\|exception" backend.log 2>/dev/null || echo 0)
    echo "Backend log errors: $ERRORS"
else
    echo "No backend.log found"
fi

echo ""
echo "=== Verification Complete ==="
