import httpx
import os
from typing import List, Dict
from backend.utils.model_info import get_model_metadata

async def fetch_free_models(client: httpx.AsyncClient) -> List[Dict]:
    """
    Dynamically fetch models from Alibaba DashScope API.
    Note: DashScope doesn't return pricing in the models list,
    so we filter for known high-value/trial-friendly chat models.
    """
    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('DASHSCOPE_API_KEY')}",
    }

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        models = data.get("data", [])
        results = []

        # Primary models we want to track
        CORE_PATTERNS = ["qwen-max", "qwen-plus", "qwen-turbo", "qwen-long", "qwen-omni", "qwen2.5-72b", "qwen-vl-max"]

        for m in models:
            m_id = m.get("id", "").lower()
            # Check if it matches our core flagship patterns
            if any(p in m_id for p in CORE_PATTERNS):
                # ENRICHMENT VIA CENTRAL UTILITY
                metadata = get_model_metadata(m_id)

                results.append({
                    "id": m["id"],
                    "name": m["id"].replace("-", " ").title(),
                    "provider": "DashScope",
                    "context_length": 131072 if "plus" in m_id or "turbo" in m_id else 32768,
                    "max_output_tokens": 8192,
                    "description": metadata["description"],
                    "description_cn": metadata["description_cn"],
                    "pricing": "Free/Trial"
                })

        # Sort to put flagship models first
        results.sort(key=lambda x: "max" in x["id"], reverse=True)
        return results
    except Exception as e:
        print(f"Error fetching DashScope models: {e}")
        return []

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()
    proxy = os.getenv("https_proxy") or os.getenv("http_proxy")
    async def _run():
        async with httpx.AsyncClient(proxy=proxy, timeout=10.0) as c:
            return await fetch_free_models(c)
    models = asyncio.run(_run())
    print(f"Loaded {len(models)} models for DashScope")
