#!/bin/bash
echo "--- Verifying Project ModelScout ---"
echo "Date: $(date)"

# 1. Backend Check
echo "[Backend] Checking health..."
if curl -s http://localhost:8000/health | grep -q "alive"; then
    echo "✅ Backend is alive."
else
    echo "❌ Backend is NOT responding or NOT running."
fi

echo "[Backend] Recent Logs (Last 5 errors):"
grep -i "error\|exception" backend.log | tail -n 5 || echo "No errors found."

# 2. Frontend Check
echo "[Frontend] Checking linting..."
cd frontend
npm run lint > lint_output.txt 2>&1
LINT_EXIT=$?
if [ $LINT_EXIT -eq 0 ]; then
    echo "✅ Frontend linting passed."
else
    echo "❌ Frontend linting FAILED (Exit Code: $LINT_EXIT)."
    echo "Top 5 issues:"
    grep -v "^$" lint_output.txt | head -n 5
fi

# 3. Code Quality / UX Suggestion (Simulated)
echo "[UX Suggestion] Next priority:"
if [ $LINT_EXIT -ne 0 ]; then
    echo "Fix React Hook issues in page.tsx to prevent cascading renders."
else
    echo "Consider adding localized time display (e.g. 'Updated 2 mins ago') for last scan."
fi

# Clean up
rm lint_output.txt
cd ..

echo "--- Verification Complete ---"
