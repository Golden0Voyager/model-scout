"""ModelScout Configuration

Static model catalog + provider discovery settings.
All models from the user's documented providers are listed here.
"""

import os
from typing import Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    id: str
    name: str
    provider: str
    context_length: int
    max_output_tokens: Optional[int] = None
    description: str = ""
    description_cn: str = ""
    capabilities: List[str] = field(default_factory=list)
    pricing_input_per_1m: Optional[float] = None
    pricing_output_per_1m: Optional[float] = None
    pricing_currency: str = "CNY"
    pricing_note: str = ""
    is_free: bool = False
    # How to probe health: "chat" | "models_endpoint" | "none"
    probe_mode: str = "chat"


@dataclass
class ProviderConfig:
    key: str
    name: str
    base_url: str
    api_key_env: str
    # Proxy strategy: "proxy" | "direct"
    network: str = "proxy"
    # Discovery mode: "static" | "dynamic"
    discovery: str = "static"
    # For dynamic discovery, the endpoint path
    models_endpoint: Optional[str] = None
    # Whether to auto-discover and add new models not in STATIC_MODELS
    auto_discover: bool = False


# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------
PROVIDERS: Dict[str, ProviderConfig] = {
    "scnet": ProviderConfig(
        key="scnet",
        name="SCNet",
        base_url="https://api.scnet.cn/api/llm/v1",
        api_key_env="SCNET_API_KEY",
        network="direct",
        discovery="dynamic",
        models_endpoint="/models",
    ),
    "aihubmix": ProviderConfig(
        key="aihubmix",
        name="AIHubMix",
        base_url="https://aihubmix.com/v1",
        api_key_env="AIHUBMIX_API_KEY",
        network="proxy",
        discovery="dynamic",
        models_endpoint="/models",
    ),
    "openrouter": ProviderConfig(
        key="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        network="proxy",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "groq": ProviderConfig(
        key="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        network="proxy",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "dashscope": ProviderConfig(
        key="dashscope",
        name="DashScope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        network="direct",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "modelscope": ProviderConfig(
        key="modelscope",
        name="ModelScope",
        base_url="https://api-inference.modelscope.cn/v1",
        api_key_env="MODELSCOPE_API_KEY",
        network="direct",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "siliconflow": ProviderConfig(
        key="siliconflow",
        name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SILICONFLOW_API_KEY",
        network="direct",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "zhipuai": ProviderConfig(
        key="zhipuai",
        name="ZhipuAI",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="ZHIPUAI_API_KEY",
        network="direct",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "deepseek": ProviderConfig(
        key="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        network="proxy",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "moonshot": ProviderConfig(
        key="moonshot",
        name="Kimi Open Platform",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        network="direct",
        discovery="static",
    ),
    "kimi_coding_plan": ProviderConfig(
        key="kimi_coding_plan",
        name="Kimi Coding Plan",
        base_url="https://api.kimi.com/coding/v1",
        api_key_env="KIMI_CODING_PLAN_API_KEY",
        network="direct",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
    "anyrouter": ProviderConfig(
        key="anyrouter",
        name="AnyRouter",
        base_url="https://api.anyrouter.net/v1",  # User must set correct endpoint in .env if available
        api_key_env="ANYROUTER_API_KEY",
        network="proxy",
        discovery="static",
    ),
    "agentrouter": ProviderConfig(
        key="agentrouter",
        name="AgentRouter",
        base_url="https://agentrouter.org/v1",
        api_key_env="AGENTROUTER_API_KEY",
        network="proxy",
        discovery="dynamic",
        models_endpoint="/models",
        auto_discover=True,
    ),
}


# ---------------------------------------------------------------------------
# Static model catalog (from user documentation)
# ---------------------------------------------------------------------------
STATIC_MODELS: List[ModelConfig] = [
    # ===== SCNet (国家超算互联网) =====
    ModelConfig(
        id="MiniMax-M2.5",
        name="MiniMax-M2.5",
        provider="scnet",
        context_length=200000,
        description="MiniMax M2.5 with 200K context window, strong Chinese and English performance.",
        description_cn="MiniMax M2.5，200K 上下文窗口，中英文能力突出。",
        capabilities=["chat", "long_context"],
        pricing_input_per_1m=1.05,
        pricing_output_per_1m=4.2,
        pricing_note="限时5折",
    ),
    ModelConfig(
        id="Qwen3-235B-A22B",
        name="Qwen3-235B-A22B",
        provider="scnet",
        context_length=32000,
        description="Alibaba Qwen3 MoE flagship. Activates 22B of 235B parameters per token. Hybrid thinking modes.",
        description_cn="阿里 Qwen3 MoE 旗舰，每 Token 激活 22B/235B 参数，支持混合思考模式。",
        capabilities=["chat", "reasoning", "moE"],
        pricing_input_per_1m=1.0,
        pricing_output_per_1m=4.0,
        pricing_note="限时优惠",
    ),
    ModelConfig(
        id="DeepSeek-R1-0528",
        name="DeepSeek-R1-0528",
        provider="scnet",
        context_length=128000,
        description="DeepSeek reasoning model optimized for math, code and multi-step logic.",
        description_cn="DeepSeek 推理模型，在数学、代码和多步逻辑推理中表现卓越。",
        capabilities=["chat", "reasoning", "coding"],
        pricing_input_per_1m=2.0,
        pricing_output_per_1m=8.0,
        pricing_note="限时优惠",
    ),
    ModelConfig(
        id="Qwen3-30B-A3B",
        name="Qwen3-30B-A3B",
        provider="scnet",
        context_length=256000,
        description="Lightweight Qwen3 MoE. Cost-efficient for high-frequency tasks.",
        description_cn="轻量 Qwen3 MoE，高频调用场景性价比极高。",
        capabilities=["chat", "moE"],
        pricing_input_per_1m=0.375,
        pricing_output_per_1m=1.5,
        pricing_note="性价比极高",
    ),
    ModelConfig(
        id="QwQ-32B",
        name="QwQ-32B",
        provider="scnet",
        context_length=32000,
        description="Alibaba reasoning-focused model via extended chain-of-thought.",
        description_cn="阿里推理专精模型，通过扩展思维链解决复杂数学和逻辑问题。",
        capabilities=["chat", "reasoning"],
        pricing_input_per_1m=0.5,
        pricing_output_per_1m=2.0,
    ),
    ModelConfig(
        id="DeepSeek-R1-Distill-Qwen-7B",
        name="DeepSeek-R1-Distill-Qwen-7B",
        provider="scnet",
        context_length=32000,
        description="Distilled R1 into Qwen 7B. Ultra-cheap for simple tasks.",
        description_cn="R1 知识蒸馏到 Qwen 7B，超低成本，适合简单任务。",
        capabilities=["chat", "reasoning"],
        pricing_input_per_1m=0.1,
        pricing_output_per_1m=0.1,
        pricing_note="超便宜",
    ),

    # ===== DeepSeek Official =====
    ModelConfig(
        id="deepseek-chat",
        name="DeepSeek-V3.2",
        provider="deepseek",
        context_length=128000,
        description="DeepSeek-V3.2 official. Price killer, matches GPT-4o class performance.",
        description_cn="DeepSeek-V3.2 官方版，价格屠夫，性能对标 GPT-4o。",
        capabilities=["chat", "coding", "reasoning"],
        pricing_input_per_1m=2.0,
        pricing_output_per_1m=3.0,
        pricing_currency="CNY",
        pricing_note="Cache Hit ¥0.2",
    ),
    ModelConfig(
        id="deepseek-reasoner",
        name="DeepSeek-R1",
        provider="deepseek",
        context_length=128000,
        description="DeepSeek-R1 official reasoning model. Exceptional math and code.",
        description_cn="DeepSeek-R1 官方推理模型，数学和代码能力卓越。",
        capabilities=["chat", "reasoning", "coding"],
        pricing_input_per_1m=2.0,
        pricing_output_per_1m=3.0,
        pricing_currency="CNY",
        pricing_note="Cache Hit ¥0.2",
    ),
    ModelConfig(
        id="deepseek-v4-flash",
        name="DeepSeek-V4 Flash",
        provider="deepseek",
        context_length=1000000,
        max_output_tokens=384000,
        description="DeepSeek-V4 Flash. 1M context, 384K max output. Lightweight variant with non-thinking and thinking (default) modes. Supports JSON Output, Tool Calls, Chat Prefix Completion and FIM Completion (Beta, non-thinking only).",
        description_cn="DeepSeek-V4 Flash，100万上下文，38.4万最大输出。V4 系列轻量版，支持非思考和思考模式（默认）。支持 JSON 输出、工具调用、聊天前缀补全和 FIM 补全（Beta，仅非思考模式）。",
        capabilities=["chat", "coding", "reasoning", "function_calling"],
        pricing_input_per_1m=0.14,
        pricing_output_per_1m=0.28,
        pricing_currency="USD",
        pricing_note="Cache Hit $0.028",
    ),
    ModelConfig(
        id="deepseek-v4-pro",
        name="DeepSeek-V4 Pro",
        provider="deepseek",
        context_length=1000000,
        max_output_tokens=384000,
        description="DeepSeek-V4 Pro. 1M context, 384K max output. Full-capability flagship with non-thinking and thinking (default) modes. Supports JSON Output, Tool Calls, Chat Prefix Completion and FIM Completion (Beta, non-thinking only).",
        description_cn="DeepSeek-V4 Pro，100万上下文，38.4万最大输出。V4 代全能力旗舰模型，支持非思考和思考模式（默认）。支持 JSON 输出、工具调用、聊天前缀补全和 FIM 补全（Beta，仅非思考模式）。",
        capabilities=["chat", "coding", "reasoning", "function_calling"],
        pricing_input_per_1m=1.74,
        pricing_output_per_1m=3.48,
        pricing_currency="USD",
        pricing_note="Cache Hit $0.145",
    ),

    # ===== Moonshot Official =====
    ModelConfig(
        id="kimi-k2.6",
        name="Kimi K2.6",
        provider="moonshot",
        context_length=256000,
        max_output_tokens=128000,
        description="Moonshot Kimi K2.6. Top-tier Chinese long-text understanding, vision and function calling.",
        description_cn="Moonshot Kimi K2.6，中文长文本理解顶级，支持视觉和函数调用。",
        capabilities=["chat", "vision", "function_calling", "long_context"],
        pricing_input_per_1m=6.84,
        pricing_output_per_1m=28.8,
        pricing_note="Cache Hit 75% off",
    ),
    ModelConfig(
        id="kimi-k2.5",
        name="Kimi K2.5",
        provider="moonshot",
        context_length=256000,
        max_output_tokens=128000,
        description="Moonshot Kimi K2.5 with strong cost-performance ratio.",
        description_cn="Moonshot Kimi K2.5，高性价比长文本模型。",
        capabilities=["chat", "vision", "function_calling", "long_context"],
        pricing_input_per_1m=4.32,
        pricing_output_per_1m=21.6,
        pricing_note="Cache Hit 75% off",
    ),
    ModelConfig(
        id="kimi-k1.5",
        name="Kimi K1.5",
        provider="moonshot",
        context_length=256000,
        max_output_tokens=128000,
        description="Moonshot Kimi K1.5 reasoning model. Strong math, code and extended thinking.",
        description_cn="Moonshot Kimi K1.5 推理模型，数学、代码和扩展思考能力强。",
        capabilities=["chat", "reasoning", "coding", "long_context"],
        pricing_input_per_1m=4.32,
        pricing_output_per_1m=18.0,
        pricing_note="Cache Hit 75% off",
    ),
    ModelConfig(
        id="kimi-latest",
        name="Kimi Latest",
        provider="moonshot",
        context_length=256000,
        max_output_tokens=128000,
        description="Moonshot Kimi latest alias. Always points to the newest stable model.",
        description_cn="Moonshot Kimi 最新版别名，始终指向最新稳定模型。",
        capabilities=["chat", "vision", "function_calling", "long_context"],
        pricing_input_per_1m=4.32,
        pricing_output_per_1m=18.0,
        pricing_note="Cache Hit 75% off",
    ),

    # ===== AnyRouter =====
    ModelConfig(
        id="gpt-5-codex",
        name="GPT-5 Codex",
        provider="anyrouter",
        context_length=128000,
        description="OpenAI GPT-5 Codex, latest coding-focused frontier model.",
        description_cn="OpenAI GPT-5 Codex，最新编程专精前沿模型。",
        capabilities=["chat", "coding", "reasoning"],
        pricing_input_per_1m=9.0,
        pricing_output_per_1m=72.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="gpt-5.3-codex",
        name="GPT-5.3 Codex",
        provider="anyrouter",
        context_length=128000,
        description="OpenAI GPT-5.3 Codex, enhanced coding and reasoning.",
        description_cn="OpenAI GPT-5.3 Codex，增强版编程和推理模型。",
        capabilities=["chat", "coding", "reasoning"],
        pricing_input_per_1m=12.6,
        pricing_output_per_1m=100.8,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-3-5-haiku-20241022",
        name="Claude 3.5 Haiku",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude 3.5 Haiku. Fast, cost-effective for light tasks.",
        description_cn="Anthropic Claude 3.5 Haiku，轻量任务快速响应。",
        capabilities=["chat", "vision"],
        pricing_input_per_1m=7.2,
        pricing_output_per_1m=36.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-3-5-sonnet-20241022",
        name="Claude 3.5 Sonnet",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude 3.5 Sonnet. Strong reasoning and coding.",
        description_cn="Anthropic Claude 3.5 Sonnet，推理和编程能力强。",
        capabilities=["chat", "vision", "coding", "reasoning"],
        pricing_input_per_1m=21.6,
        pricing_output_per_1m=108.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-3-7-sonnet-20250219",
        name="Claude 3.7 Sonnet",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude 3.7 Sonnet with extended thinking.",
        description_cn="Anthropic Claude 3.7 Sonnet，支持扩展思考模式。",
        capabilities=["chat", "vision", "coding", "reasoning"],
        pricing_input_per_1m=21.6,
        pricing_output_per_1m=108.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-haiku-4-5-20251001",
        name="Claude Haiku 4.5",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude Haiku 4.5, next-gen light model.",
        description_cn="Anthropic Claude Haiku 4.5，下一代轻量模型。",
        capabilities=["chat", "vision"],
        pricing_input_per_1m=7.2,
        pricing_output_per_1m=36.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-opus-4-1-20250805",
        name="Claude Opus 4.1",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude Opus 4.1, top-tier frontier model.",
        description_cn="Anthropic Claude Opus 4.1，顶级前沿模型。",
        capabilities=["chat", "vision", "coding", "reasoning"],
        pricing_input_per_1m=108.0,
        pricing_output_per_1m=540.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-opus-4-20250514",
        name="Claude Opus 4",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude Opus 4, predecessor to 4.1.",
        description_cn="Anthropic Claude Opus 4，4.1 的前代版本。",
        capabilities=["chat", "vision", "coding", "reasoning"],
        pricing_input_per_1m=108.0,
        pricing_output_per_1m=540.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-opus-4-5-20251101",
        name="Claude Opus 4.5",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude Opus 4.5, enhanced reasoning variant.",
        description_cn="Anthropic Claude Opus 4.5，增强推理变体。",
        capabilities=["chat", "vision", "coding", "reasoning"],
        pricing_input_per_1m=36.0,
        pricing_output_per_1m=180.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="claude-opus-4-6",
        name="Claude Opus 4-6",
        provider="anyrouter",
        context_length=200000,
        description="Anthropic Claude Opus 4-6, latest Opus generation.",
        description_cn="Anthropic Claude Opus 4-6，最新 Opus 世代。",
        capabilities=["chat", "vision", "coding", "reasoning"],
        pricing_input_per_1m=36.0,
        pricing_output_per_1m=180.0,
        pricing_note="中转定价",
    ),
    ModelConfig(
        id="gemini-2.5-pro",
        name="Gemini 2.5 Pro",
        provider="anyrouter",
        context_length=1000000,
        description="Google Gemini 2.5 Pro with 1M context and strong multimodal capabilities.",
        description_cn="Google Gemini 2.5 Pro，100万上下文，强大多模态能力。",
        capabilities=["chat", "vision", "coding", "long_context"],
        pricing_input_per_1m=9.0,
        pricing_output_per_1m=72.0,
        pricing_note="中转定价",
    ),

    # ===== AIHubMix Free Models =====
    ModelConfig(
        id="coding-glm-5.1-free",
        name="GLM-5.1 Coding (Free)",
        provider="aihubmix",
        context_length=200000,
        max_output_tokens=128000,
        description="Z.AI GLM-5.1 coding-specialized, long context.",
        description_cn="Z.AI GLM-5.1 编码专精版，长上下文。",
        capabilities=["chat", "coding", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
    ),
    ModelConfig(
        id="coding-glm-5-free",
        name="GLM-5 Coding (Free)",
        provider="aihubmix",
        context_length=200000,
        max_output_tokens=128000,
        description="Z.AI GLM-5 general coding model.",
        description_cn="Z.AI GLM-5 通用编码模型。",
        capabilities=["chat", "coding", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
    ),
    ModelConfig(
        id="coding-glm-5-turbo-free",
        name="GLM-5 Turbo Coding (Free)",
        provider="aihubmix",
        context_length=200000,
        max_output_tokens=128000,
        description="Z.AI GLM-5 Turbo, optimized for OpenClaw.",
        description_cn="Z.AI GLM-5 Turbo，针对 OpenClaw 优化。",
        capabilities=["chat", "coding", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
    ),
    ModelConfig(
        id="kimi-for-coding-free",
        name="Kimi for Coding (Free)",
        provider="aihubmix",
        context_length=256000,
        max_output_tokens=128000,
        description="Moonshot Kimi coding-specialized.",
        description_cn="Moonshot Kimi 编码专精版。",
        capabilities=["chat", "coding", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
    ),
    ModelConfig(
        id="k2.6-code-preview-free",
        name="K2.6 Code Preview (Free)",
        provider="aihubmix",
        context_length=256000,
        max_output_tokens=128000,
        description="Kimi K2.6 code preview edition.",
        description_cn="Kimi K2.6 代码预览版。",
        capabilities=["chat", "coding", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
    ),
    ModelConfig(
        id="gemini-3-flash-preview-free",
        name="Gemini 3 Flash Preview (Free)",
        provider="aihubmix",
        context_length=1000000,
        max_output_tokens=64000,
        description="Gemini 3 Flash preview, 1M context.",
        description_cn="Gemini 3 Flash 预览版，100万上下文。",
        capabilities=["chat", "vision", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
        probe_mode="none",
    ),
    ModelConfig(
        id="coding-minimax-m2.7-free",
        name="MiniMax M2.7 Coding (Free)",
        provider="aihubmix",
        context_length=204000,
        max_output_tokens=128000,
        description="MiniMax M2.7 coding model.",
        description_cn="MiniMax M2.7 编码模型。",
        capabilities=["chat", "coding", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
    ),
    ModelConfig(
        id="qwen3.6-plus-preview-free",
        name="Qwen 3.6 Plus Preview (Free)",
        provider="aihubmix",
        context_length=1000000,
        max_output_tokens=655000,
        description="Qwen 3.6 Plus preview, ultra-long context.",
        description_cn="Qwen 3.6 Plus 预览版，超长上下文。",
        capabilities=["chat", "long_context"],
        is_free=True,
        pricing_note="Rate limited",
        probe_mode="none",
    ),

    # ===== AgentRouter =====
    ModelConfig(
        id="claude-haiku-4-5-20251001",
        name="Claude Haiku 4.5",
        provider="agentrouter",
        context_length=200000,
        description="Anthropic Claude Haiku 4.5 via AgentRouter. Fast, cost-effective for light tasks.",
        description_cn="Anthropic Claude Haiku 4.5 (AgentRouter 中转)，轻量任务快速响应。",
        capabilities=["chat", "vision"],
        pricing_input_per_1m=2.0,
        pricing_output_per_1m=4.0,
        pricing_currency="USD",
        pricing_note="AgentRouter pricing",
    ),
    ModelConfig(
        id="claude-opus-4-6",
        name="Claude Opus 4-6",
        provider="agentrouter",
        context_length=200000,
        description="Anthropic Claude Opus 4-6 via AgentRouter. Top-tier frontier model.",
        description_cn="Anthropic Claude Opus 4-6 (AgentRouter 中转)，顶级前沿模型。",
        capabilities=["chat", "vision", "coding", "reasoning"],
        pricing_input_per_1m=21.0,
        pricing_output_per_1m=105.0,
        pricing_currency="USD",
        pricing_note="AgentRouter pricing",
    ),
    ModelConfig(
        id="deepseek-r1-0528",
        name="DeepSeek R1-0528",
        provider="agentrouter",
        context_length=128000,
        description="DeepSeek R1-0528 via AgentRouter. Reasoning model optimized for math and code.",
        description_cn="DeepSeek R1-0528 (AgentRouter 中转)，推理模型，数学和代码能力卓越。",
        capabilities=["chat", "reasoning", "coding"],
        pricing_input_per_1m=0.3,
        pricing_output_per_1m=0.045,
        pricing_currency="USD",
        pricing_note="AgentRouter pricing",
    ),
]


def get_provider_config(key: str) -> Optional[ProviderConfig]:
    return PROVIDERS.get(key)


def get_static_models() -> List[ModelConfig]:
    return STATIC_MODELS


def get_models_for_provider(provider_key: str) -> List[ModelConfig]:
    return [m for m in STATIC_MODELS if m.provider == provider_key]
