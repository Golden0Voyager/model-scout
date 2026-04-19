import httpx
import os
from typing import List, Dict
from backend.utils.model_info import get_model_metadata

async def fetch_free_models(client: httpx.AsyncClient) -> List[Dict]:
    """
    Dynamically fetch models from Zhipu AI (BigModel) API.
    """
    url = "https://open.bigmodel.cn/api/paas/v4/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('ZHIPUAI_API_KEY')}",
    }

    try:
        try:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                print(f"Zhipu API models endpoint status {response.status_code}, using fallback list.")
                model_ids = ["glm-4-flash", "glm-4.5-air"]
            else:
                data = response.json()
                model_ids = [m["id"] for m in data.get("data", []) if "glm" in m["id"].lower()]
        except Exception as e:
            print(f"Zhipu API request failed: {e}, falling back to core models.")
            model_ids = ["glm-4-flash", "glm-4.5-air"]

        if not model_ids:
            model_ids = ["glm-4-flash", "glm-4.5-air"]

        results = []
        for m_id in model_ids:
            # flash/air/turbo variants are free-tier friendly
            is_free = any(tag in m_id.lower() for tag in ["flash", "air", "turbo"])
            metadata = get_model_metadata(m_id)
            results.append({
                "id": m_id,
                "name": m_id.upper(),
                "provider": "ZhipuAI",
                "context_length": 128000,
                "max_output_tokens": 4096,
                "description": metadata["description"],
                "description_cn": metadata["description_cn"],
                "pricing": "Free" if is_free else "Trial"
            })

        return results
    except Exception as e:
        print(f"Error fetching Zhipu models: {e}")
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
    print(f"Found {len(models)} models on Zhipu AI")
    for m in models:
        print(f"  - {m['id']} ({m['pricing']})")
