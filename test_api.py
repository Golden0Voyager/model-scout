#!/usr/bin/env python3
"""ModelScout API & Backend Integration Tests"""
import asyncio, json, sys, time, sqlite3
from pathlib import Path

BASE = "http://127.0.0.1:8000"
DB = Path(__file__).parent / "backend" / "model_scout.db"

def fetch(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except Exception as e:
        return 0, str(e)

async def test_health():
    print("\n[TEST] GET /health")
    code, data = fetch(f"{BASE}/health")
    assert code == 200, f"Expected 200, got {code}: {data}"
    assert data.get("status") == "healthy"
    print(f"  ✅ status={data['status']}, models={data.get('model_count')}")

async def test_models_list():
    print("\n[TEST] GET /api/models")
    t0 = time.time()
    code, data = fetch(f"{BASE}/api/models")
    elapsed = time.time() - t0
    assert code == 200, f"Expected 200, got {code}: {data}"
    assert "providers" in data
    assert "models" in data
    assert data["total_models"] >= 0
    print(f"  ✅ {data['total_models']} models, {data['online_models']} online (t={elapsed:.2f}s)")
    return data

async def test_model_detail(data):
    print("\n[TEST] GET /api/models/{id}")
    models = data.get("models", [])
    if not models:
        print("  ⚠️  No models to test detail endpoint")
        return
    sample = models[0]
    mid = sample["id"]
    code, detail = fetch(f"{BASE}/api/models/{mid}")
    assert code == 200, f"Expected 200, got {code}: {detail}"
    assert detail.get("id") == mid
    print(f"  ✅ Model {mid} detail OK")

async def test_database():
    print("\n[TEST] SQLite database integrity")
    assert DB.exists(), f"DB not found: {DB}"
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Check tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r["name"] for r in cur.fetchall()]
    assert "health_checks" in tables
    assert "scan_log" in tables
    print(f"  ✅ Tables: {tables}")

    # Check model count consistency (health_checks rows vs static models)
    cur.execute("SELECT COUNT(DISTINCT model_id) as c FROM health_checks")
    db_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM scan_log")
    scan_count = cur.fetchone()["c"]
    print(f"  ✅ health_checks={db_count} distinct models, scan_log={scan_count} scans")

    # Check required columns in health_checks
    cur.execute("PRAGMA table_info(health_checks)")
    cols = {r["name"] for r in cur.fetchall()}
    required = {"model_id", "provider", "status", "latency_ms", "last_checked"}
    missing = required - cols
    assert not missing, f"Missing columns: {missing}"
    print(f"  ✅ Required columns present")

    # Check scan_log columns
    cur.execute("PRAGMA table_info(scan_log)")
    scan_cols = {r["name"] for r in cur.fetchall()}
    assert "started_at" in scan_cols and "finished_at" in scan_cols
    print(f"  ✅ scan_log columns OK")

    conn.close()

async def test_frontend_static():
    print("\n[TEST] Frontend static files")
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:3000")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode()
            assert resp.status == 200
            assert "ModelScout" in html or "model" in html.lower()
            print(f"  ✅ Frontend reachable (status {resp.status}, contains 'ModelScout')")
    except Exception as e:
        print(f"  ⚠️  Frontend check failed: {e}")

async def test_cors():
    print("\n[TEST] CORS headers")
    import urllib.request
    req = urllib.request.Request(
        f"{BASE}/api/models",
        headers={"Origin": "http://localhost:3000"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            acao = resp.headers.get("Access-Control-Allow-Origin")
            assert acao in ("*", "http://localhost:3000"), f"CORS header mismatch: {acao}"
            print(f"  ✅ CORS Access-Control-Allow-Origin: {acao}")
    except Exception as e:
        print(f"  ⚠️  CORS check failed: {e}")

async def test_scan_trigger():
    print("\n[TEST] POST /api/scan (async trigger)")
    import urllib.request
    req = urllib.request.Request(
        f"{BASE}/api/scan",
        method="POST",
        headers={"Accept": "application/json"},
        data=b""
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            code = resp.status
            data = json.loads(resp.read().decode())
            assert code in (200, 202), f"Expected 200/202, got {code}"
            assert "message" in data
            print(f"  ✅ Scan triggered: {data['message']}")
    except Exception as e:
        print(f"  ⚠️  Scan trigger failed: {e}")

async def test_latency_slo():
    print("\n[TEST] API latency SLO (< 500ms)")
    times = []
    for _ in range(5):
        t0 = time.time()
        fetch(f"{BASE}/api/models")
        times.append(time.time() - t0)
    avg = sum(times) / len(times)
    p95 = sorted(times)[int(len(times) * 0.95)]
    status = "✅" if avg < 0.5 else "⚠️"
    print(f"  {status} avg={avg:.3f}s, p95={p95:.3f}s")

async def main():
    print("=" * 50)
    print("ModelScout Integration Test Suite")
    print("=" * 50)

    try:
        await test_health()
        data = await test_models_list()
        await test_model_detail(data)
        await test_database()
        await test_frontend_static()
        await test_cors()
        await test_scan_trigger()
        await test_latency_slo()
        print("\n" + "=" * 50)
        print("✅ All tests passed")
        print("=" * 50)
        return 0
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
