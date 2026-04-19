import httpx
import os
from typing import List, Dict
from backend.utils.model_info import get_model_metadata

async def fetch_free_models(client: httpx.AsyncClient) -> List[Dict]:
    """
    Fetch models from SiliconFlow.
    SiliconFlow provides a wide range of free-tier models.
    """
    url = "https://api.siliconflow.cn/v1/models"
    headers = {
        "Authorization": f"Bearer {os.getenv('SILICONFLOW_API_KEY')}",
        "Content-Type": "application/json"
    }

    # SiliconFlow free-tier model identifiers as of current status
    FREE_MODELS_IDS = [
        "deepseek-ai/DeepSeek-V3",
        "deepseek-ai/DeepSeek-V3.2",
        "deepseek-ai/DeepSeek-R1",
        "deepseek-ai/DeepSeek-V2.5",
        "Qwen/Qwen3-32B",
        "Qwen/Qwen3-8B",
        "Qwen/Qwen2.5-72B-Instruct",
        "Qwen/Qwen2.5-14B-Instruct",
        "Qwen/Qwen2.5-7B-Instruct",
        "internlm/internlm2_5-7b-chat",
        "THUDM/GLM-4-9B-0414",
        "zai-org/GLM-4.5-Air",
        "stepfun-ai/Step-3.5-Flash"
    ]

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        models = data.get("data", [])
        results = []

        for m in models:
            m_id = m.get("id")
            # We filter by our curated free list or if the ID implies free
            if m_id in FREE_MODELS_IDS:
                # ENRICHMENT VIA CENTRAL UTILITY
                metadata = get_model_metadata(m_id)

                results.append({
                    "id": m_id,
                    "name": m_id.split("/")[-1],
                    "provider": "SiliconFlow",
                    "context_length": 32768,
                    "max_output_tokens": 4096,
                    "description": metadata["description"],
                    "description_cn": metadata["description_cn"],
                    "pricing": "Free"
                })

        return results
    except Exception as e:
        print(f"Error fetching SiliconFlow models: {e}")
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
    print(f"Found {len(models)} free models on SiliconFlow")
