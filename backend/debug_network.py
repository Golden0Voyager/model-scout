import httpx
import asyncio
import os
from dotenv import load_dotenv

async def test_connectivity():
    load_dotenv()
    url = "https://openrouter.ai/api/v1/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
    }
    
    print(f"Testing connectivity to {url}...")
    print(f"Env Proxies: HTTP={os.getenv('http_proxy')}, HTTPS={os.getenv('https_proxy')}")
    
    # Test 1: Default client (should use env vars)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            print(f"Test 1 (Default): Status {resp.status_code}")
    except Exception as e:
        print(f"Test 1 (Default) FAILED: {e}")

    # Test 2: Explicit proxy
    proxy = os.getenv('https_proxy') or os.getenv('http_proxy')
    if proxy:
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                print(f"Test 2 (Explicit Proxy): Status {resp.status_code}")
        except Exception as e:
            print(f"Test 2 (Explicit Proxy) FAILED: {e}")
    else:
        print("Test 2 skipped (no proxy env found)")

if __name__ == "__main__":
    asyncio.run(test_connectivity())
