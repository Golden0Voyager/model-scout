import httpx
import os
from typing import List, Dict
from backend.utils.model_info import get_model_metadata

async def fetch_free_models(client: httpx.AsyncClient) -> List[Dict]:
    """
    Fetch free models from AIHubMix.
    AIHubMix is an OpenAI-compatible aggregator. Free models are identified
    by pricing fields returning 0.
    """
    url = "https://aihubmix.com/v1/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('AIHUBMIX_API_KEY')}",
    }

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        models = data.get("data", [])
        results = []

        for m in models:
            m_id = m.get("id", "")

            # AIHubMix marks free models with "-free" suffix
            is_free = m_id.lower().endswith("-free")

            # Also check pricing fields if present
            if not is_free:
                pricing = m.get("pricing", {})
                if pricing:
                    prompt_price = float(pricing.get("prompt", 1))
                    completion_price = float(pricing.get("completion", 1))
                    is_free = (prompt_price == 0 and completion_price == 0)

            if is_free:
                metadata = get_model_metadata(m_id)
                # 去掉 -free 后缀作为显示名称
                display_name = m.get("name", m_id)
                if display_name.lower().endswith("-free"):
                    display_name = display_name[:-5]
                results.append({
                    "id": m_id,
                    "name": display_name,
                    "provider": "AIHubMix",
                    "context_length": m.get("context_length", 32768),
                    "max_output_tokens": 4096,
                    "description": metadata["description"],
                    "description_cn": metadata["description_cn"],
                    "pricing": "Free"
                })

        return results
    except Exception as e:
        print(f"Error fetching AIHubMix models: {e}")
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
    print(f"Found {len(models)} free models on AIHubMix")
    for m in models[:10]:
        print(f"- {m['id']} ({m['name']})")
