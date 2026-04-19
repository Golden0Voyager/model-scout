import asyncio
import time
import os
from typing import Dict
from openai import AsyncOpenAI
import httpx

class Benchmarker:
    def __init__(self, proxy: str = None):
        # 海外 API — 走代理
        self._proxy_http = httpx.AsyncClient(
            proxy=proxy, timeout=30.0, follow_redirects=True
        )
        # 国内 API — 直连
        self._direct_http = httpx.AsyncClient(
            timeout=30.0, follow_redirects=True
        )

        # 海外平台
        self.openrouter_client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            http_client=self._proxy_http
        )
        self.groq_client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
            http_client=self._proxy_http
        )
        self.aihubmix_client = AsyncOpenAI(
            base_url="https://aihubmix.com/v1",
            api_key=os.getenv("AIHUBMIX_API_KEY"),
            http_client=self._proxy_http
        )

        # 国内平台
        self.dashscope_client = AsyncOpenAI(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            http_client=self._direct_http
        )
        self.siliconflow_client = AsyncOpenAI(
            base_url="https://api.siliconflow.cn/v1",
            api_key=os.getenv("SILICONFLOW_API_KEY"),
            http_client=self._direct_http
        )
        self.zhipu_client = AsyncOpenAI(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key=os.getenv("ZHIPUAI_API_KEY"),
            http_client=self._direct_http
        )
        self.scnet_client = AsyncOpenAI(
            base_url="https://api.scnet.cn/api/llm/v1",
            api_key=os.getenv("SCNET_API_KEY"),
            http_client=self._direct_http
        )

        self._provider_map = {
            "OpenRouter": self.openrouter_client,
            "Groq": self.groq_client,
            "DashScope": self.dashscope_client,
            "SiliconFlow": self.siliconflow_client,
            "ZhipuAI": self.zhipu_client,
            "AIHubMix": self.aihubmix_client,
            "SCNET": self.scnet_client,
        }

    async def close(self):
        await self._proxy_http.aclose()
        await self._direct_http.aclose()

    async def run_test(self, model_id: str, provider: str, prompt: str = "Introduce yourself in 30 words.") -> Dict:
        """
        Run a single benchmark test for a model.
        """
        # Skip non-chat models that likely don't support streaming completions
        skip_keywords = ["guard", "classification", "rerank", "moderation", "embedding", "whisper", "vision-encoder"]
        if any(kw in model_id.lower() for kw in skip_keywords):
            return {"status": "error", "message": "Non-chat model"}

        client = self._provider_map.get(provider)
        if not client:
            return {"status": "error", "message": f"Unknown provider: {provider}"}

        start_time = time.perf_counter()
        ttft = 0.0
        chunk_count = 0
        first_token_received = False
        final_usage_tokens = None

        try:
            response = await client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                max_tokens=100
            )

            async for chunk in response:
                if not first_token_received:
                    ttft = time.perf_counter() - start_time
                    first_token_received = True

                if chunk.choices and chunk.choices[0].delta.content:
                    chunk_count += 1

                # 部分 provider 在最后一个 chunk 返回 usage 统计
                if hasattr(chunk, "usage") and chunk.usage:
                    ct = getattr(chunk.usage, "completion_tokens", None)
                    if ct:
                        final_usage_tokens = ct

            end_time = time.perf_counter()
            total_tokens = final_usage_tokens or chunk_count
            duration = end_time - (start_time + ttft)
            tps = total_tokens / duration if duration > 0 else 0

            return {
                "status": "success",
                "ttft": round(ttft, 3),
                "tps": round(tps, 2),
                "total_tokens": total_tokens,
                "total_time": round(end_time - start_time, 3)
            }
        except Exception as e:
            msg = str(e)
            if "429" in msg:
                msg = "Rate Limit (429)"
            elif "502" in msg or "500" in msg:
                msg = "Provider Error (5xx)"
            return {"status": "error", "message": msg}

async def main():
    from dotenv import load_dotenv
    load_dotenv()
    proxy = os.getenv("https_proxy") or os.getenv("http_proxy")
    bench = Benchmarker(proxy=proxy)
    # Test a known fast model on Groq
    print("Testing Groq llama-3.3-70b-versatile...")
    result = await bench.run_test("llama-3.3-70b-versatile", "Groq")
    print(f"Result: {result}")
    await bench.close()

if __name__ == "__main__":
    asyncio.run(main())
