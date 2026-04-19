import httpx
import os
from typing import List, Dict
from backend.utils.model_info import get_model_metadata

# SCNET models that are non-chat (embedding, OCR, etc.) — skip for benchmarking
_SKIP_MODELS = {"OCR", "Qwen3-Embedding-8B"}

async def fetch_free_models(client: httpx.AsyncClient) -> List[Dict]:
    """
    Fetch models from SCNET (中国超算互联网).
    All models on the platform are free (算力资源领取).
    OpenAI-compatible API at https://api.scnet.cn/api/llm/v1
    """
    url = "https://api.scnet.cn/api/llm/v1/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('SCNET_API_KEY')}",
    }

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        models = data.get("data", [])
        results = []

        for m in models:
            m_id = m.get("id", "")
            if m_id in _SKIP_MODELS:
                continue

            metadata = get_model_metadata(m_id)
            results.append({
                "id": m_id,
                "name": m_id,
                "provider": "SCNET",
                "context_length": 32768,
                "max_output_tokens": 4096,
                "description": metadata["description"],
                "description_cn": metadata["description_cn"],
                "pricing": "Free (算力资源)"
            })

        return results
    except Exception as e:
        print(f"Error fetching SCNET models: {e}")
        return []

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()
    async def _run():
        async with httpx.AsyncClient(timeout=10.0) as c:
            return await fetch_free_models(c)
    models = asyncio.run(_run())
    print(f"Found {len(models)} models on SCNET")
    for m in models:
        print(f"- {m['id']}")
