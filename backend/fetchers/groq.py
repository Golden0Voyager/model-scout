import httpx
import os
from typing import List, Dict
from backend.utils.model_info import get_model_metadata

async def fetch_free_models(client: httpx.AsyncClient) -> List[Dict]:
    """
    Fetch models from Groq. All currently active Groq models are treated
    as 'testable' for the free tier (RPM limits apply).
    """
    url = "https://api.groq.com/openai/v1/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
        "Content-Type": "application/json"
    }

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        models = data.get("data", [])
        results = []

        for m in models:
            m_id = m.get("id")
            # ENRICHMENT VIA CENTRAL UTILITY
            metadata = get_model_metadata(m_id)

            results.append({
                "id": m_id,
                "name": m_id,
                "provider": "Groq",
                "context_length": 32768,
                "max_output_tokens": 8192,
                "description": metadata["description"],
                "description_cn": metadata["description_cn"],
                "pricing": "Free (Beta/Quota)"
            })

        return results
    except Exception as e:
        print(f"Error fetching Groq models: {e}")
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
    print(f"Found {len(models)} models on Groq")
