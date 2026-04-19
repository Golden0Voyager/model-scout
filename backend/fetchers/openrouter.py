import httpx
import os
from typing import List, Dict
from backend.utils.model_info import get_model_metadata

async def fetch_free_models(client: httpx.AsyncClient) -> List[Dict]:
    """
    Fetch free models from OpenRouter API.
    """
    url = "https://openrouter.ai/api/v1/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "HTTP-Referer": "https://github.com/OpenRouter/ModelScout",
        "X-Title": "ModelScout"
    }

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        models = data.get("data", [])
        free_models = []

        for m in models:
            pricing = m.get("pricing", {})
            # OpenRouter marks free models with 0 price for both prompt and completion
            if float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0:
                model_id = m.get("id", "")
                raw_desc = m.get("description", "")

                # ENRICHMENT VIA CENTRAL UTILITY
                metadata = get_model_metadata(model_id, raw_desc)

                top_provider = m.get("top_provider", {})
                max_output = top_provider.get("max_completion_tokens", 4096)

                free_models.append({
                    "id": model_id,
                    "name": m.get("name"),
                    "provider": "OpenRouter",
                    "context_length": m.get("context_length"),
                    "max_output_tokens": max_output,
                    "description": metadata["description"],
                    "description_cn": metadata["description_cn"],
                    "pricing": "Free"
                })

        return free_models
    except Exception as e:
        print(f"Error fetching OpenRouter models: {e}")
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
    print(f"Found {len(models)} free models on OpenRouter")
    for m in models[:5]:
        print(f"- {m['id']} ({m['name']})")
