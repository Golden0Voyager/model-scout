import re
from typing import Dict, Optional

# High-fidelity Model Intelligence Metadata
# Standardized across all providers (SiliconFlow, OpenRouter, ZhipuAI, DashScope, Groq, AIHubMix, SCNET)
MODEL_DATA = {
    "deepseek-v3": {
        "description": "DeepSeek-V3 is a strong Mixture-of-Experts (MoE) language model with 671B total parameters, of which 37B are activated per token. It employs Multi-head Latent Attention (MLA) and FP8 mixed precision training to achieve superior performance across math, code, and reasoning benchmarks while maintaining high inference efficiency.",
        "description_cn": "DeepSeek-V3 是深度求索（DeepSeek）推出的高性能混合专家模型（MoE），总参数量 671B，推理时每个 Token 仅激活 37B 参数。该模型采用了多头潜在注意力（MLA）架构和 FP8 混合精度训练，在数学、代码及逻辑推理等多项基准测试中展现出顶尖的开源性能。"
    },
    "deepseek-r1": {
        "description": "DeepSeek-R1 is a reasoning-optimized model that uses Large-Scale Reinforcement Learning (RL) to enhance complex problem-solving. It demonstrates exceptional performance in mathematics, programming, and complicated logical tasks, rivaling closed-source frontier models in multi-step reasoning capabilities.",
        "description_cn": "DeepSeek-R1 是一款专注于推理能力的模型，通过大规模强化学习（RL）显著增强了其处理复杂问题的能力。在数学、编程及复杂逻辑任务中表现卓越，其多步推理能力可与顶级闭源旗舰模型相媲美。"
    },
    "deepseek-r1-distill": {
        "description": "DeepSeek-R1-Distill models are knowledge-distilled versions of DeepSeek-R1, transferring its strong reasoning patterns into smaller, faster architectures (Qwen/Llama backbones). They retain most of R1's reasoning ability at a fraction of the compute cost.",
        "description_cn": "DeepSeek-R1-Distill 系列是 DeepSeek-R1 的知识蒸馏版本，将 R1 的强推理能力迁移到更小更快的架构（Qwen/Llama 骨干）中。以极低的算力成本保留了 R1 的大部分推理能力。"
    },
    "llama-3.3": {
        "description": "Llama 3.3 70B is Meta's state-of-the-art open-weights model, providing capabilities comparable to Llama 3 405B but at a fraction of the compute cost. It features optimized multilingual support, enhanced reasoning, and improved instruction-following, making it a versatile choice for high-end applications.",
        "description_cn": "Llama 3.3 70B 是 Meta 推出的顶级开源权重模型，以极低的计算成本实现了与 Llama 3 405B 相当的能力。它具备优化的多语言支持、更强的逻辑推理和更精准的指令遵循，是高端应用场景的理想选择。"
    },
    "gemma": {
        "description": "Google Gemma is a family of lightweight, open models built from the same research as Gemini. They feature state-of-the-art performance relative to their size, with MoE variants activating only a fraction of total parameters during inference for high efficiency.",
        "description_cn": "Google Gemma 系列是基于 Gemini 研究成果构建的轻量化开源模型。相对于其参数规模，具备顶尖性能，MoE 变体在推理时仅激活部分参数以实现高效计算。"
    },
    "qwen3": {
        "description": "Qwen3 is Alibaba's latest generation model family, featuring both dense and MoE architectures. The flagship Qwen3-235B-A22B is a MoE model activating only 22B of 235B parameters per token. Qwen3 introduces hybrid thinking modes — enabling both deep reasoning and fast response within a single model.",
        "description_cn": "Qwen3 是阿里云通义千问最新一代模型，涵盖稠密和 MoE 两种架构。旗舰 Qwen3-235B-A22B 是 MoE 模型，每 Token 仅激活 235B 中的 22B 参数。Qwen3 引入了混合思考模式——在单一模型内兼顾深度推理和快速响应。"
    },
    "qwen2.5": {
        "description": "Qwen2.5 is Alibaba's flagship large language model, featuring significant improvements in mathematical reasoning, coding, and multilingual understanding. It covers a wide range of model sizes (0.5B to 72B), optimized for both edge devices and massive cloud-scale workloads with high reliability.",
        "description_cn": "Qwen2.5 是阿里云通义千问系列的旗舰模型，在数学推理、代码生成及多语言理解方面有显著提升。覆盖了从 0.5B 到 72B 的全尺寸参数，针对端侧设备和云端大规模工作负载进行了深度优化，具备极高的可靠性。"
    },
    "qwq": {
        "description": "QwQ (Qwen with Questions) is Alibaba's reasoning-focused model that excels at complex mathematical and logical problem-solving through extended chain-of-thought reasoning. It achieves competitive performance with frontier reasoning models.",
        "description_cn": "QwQ（Qwen with Questions）是阿里云推出的推理专精模型，通过扩展的思维链推理在复杂数学和逻辑问题求解中表现卓越，其推理性能可媲美前沿推理模型。"
    },
    "glm-4": {
        "description": "GLM-4 is Zhipu AI's fourth-generation flagship model series. It features strong multimodal capabilities, enhanced tool-calling precision, and sophisticated logical reasoning. It is designed to match the performance of international frontier models while providing deep optimization for Chinese language and culture.",
        "description_cn": "GLM-4 是智谱 AI 的第四代旗舰大模型系列。具备强大的多模态能力、精准的工具调用（Tool-calling）以及严密的逻辑推理。在性能对标国际顶级模型的同时，针对中文语境和文化进行了深度优化。"
    },
    "glm-4.5": {
        "description": "GLM-4.5 is Zhipu AI's latest generation model with improved reasoning, instruction-following, and multimodal understanding. The Air variant provides a cost-effective option while maintaining strong performance.",
        "description_cn": "GLM-4.5 是智谱 AI 的最新一代模型，在推理、指令遵循和多模态理解方面全面提升。Air 变体提供高性价比选项，同时保持强大性能。"
    },
    "glm-5": {
        "description": "GLM-5 is Zhipu AI's fifth-generation flagship model series, representing a major leap in reasoning, tool-use, and multimodal capabilities. The Turbo variant optimizes for speed while maintaining frontier-level quality.",
        "description_cn": "GLM-5 是智谱 AI 第五代旗舰大模型，在推理、工具调用和多模态能力方面实现重大飞跃。Turbo 变体在保持顶尖质量的同时优化了响应速度。"
    },
    "mixtral-8x7b": {
        "description": "Mixtral 8x7B is a high-quality sparse mixture-of-experts model (SMoE) by Mistral AI. It uses a 45B parameter backbone but only activates ~12B parameters during inference, outperforming Llama 2 70B on most benchmarks with 6x faster inference speeds.",
        "description_cn": "Mixtral 8x7B 是由 Mistral AI 开发的高质量稀疏混合专家模型（SMoE）。虽然拥有 45B 的总参数量，但推理时仅激活约 12B 参数，在多数基准测试中超越了 Llama 2 70B，且推理速度快 6 倍。"
    },
    "phi-3": {
        "description": "Phi-3 is a family of small language models (SLMs) from Microsoft. Despite their compact size, they exhibit performance competitive with models multiple times larger, thanks to their training on high-quality 'textbook grade' data and synthetic reasoning datasets.",
        "description_cn": "Phi-3 是微软推出的轻量级大模型（SLM）系列。得益于高质量的「教科书级」数据和合成推理数据集的训练，尽管体积小巧，其性能却足以抗衡数倍于其参数规模的大型模型。"
    },
    "minimax": {
        "description": "MiniMax models are developed by MiniMax AI, featuring strong performance in Chinese and English language tasks, creative writing, and conversational AI. The M2.5 series represents their latest architecture with improved reasoning capabilities.",
        "description_cn": "MiniMax 系列模型由 MiniMax（稀宇科技）开发，在中英文语言任务、创意写作和对话 AI 领域表现突出。M2.5 系列采用最新架构，推理能力显著提升。"
    },
    "internlm": {
        "description": "InternLM is developed by Shanghai AI Laboratory. The InternLM2.5 series features enhanced reasoning, long-context understanding, and tool-use capabilities, providing competitive open-source performance for both research and applications.",
        "description_cn": "InternLM（书生）由上海人工智能实验室开发。InternLM2.5 系列在推理能力、长上下文理解和工具使用方面均有显著提升，为科研和应用提供了极具竞争力的开源性能。"
    },
    "step": {
        "description": "Step models are developed by StepFun AI, featuring efficient architectures optimized for fast inference. The Flash variants prioritize low latency while maintaining strong reasoning and instruction-following capabilities.",
        "description_cn": "Step 系列模型由阶跃星辰（StepFun）开发，采用高效架构优化推理速度。Flash 变体在保持强推理和指令遵循能力的同时，优先追求低延迟响应。"
    },
}

# Ordered matching rules: (pattern_in_id, data_key)
_MATCH_RULES = [
    ("deepseek-r1-distill", "deepseek-r1-distill"),
    ("deepseek-r1", "deepseek-r1"),
    ("deepseek-v3", "deepseek-v3"),
    ("llama-3.3", "llama-3.3"),
    ("qwen3", "qwen3"),
    ("qwq", "qwq"),
    ("qwen2.5", "qwen2.5"),
    ("glm-5", "glm-5"),
    ("glm-4.5", "glm-4.5"),
    ("glm-4", "glm-4"),
    ("gemma", "gemma"),
    ("mixtral", "mixtral-8x7b"),
    ("phi-3", "phi-3"),
    ("minimax", "minimax"),
    ("internlm", "internlm"),
    ("step", "step"),
]

# Broader family fallbacks
_FAMILY_FALLBACKS = {
    "llama": {
        "description": "Meta Llama series: High-performance open-weights models optimized for reasoning and dialogue.",
        "description_cn": "Meta Llama 系列：针对推理和对话进行了深度优化的高性能开源权重模型。"
    },
    "qwen": {
        "description": "Alibaba Qwen series: State-of-the-art models with exceptional performance in reasoning and Chinese tasks.",
        "description_cn": "阿里云通义千问系列：在逻辑推理和中文任务中表现卓越的世界级模型。"
    },
    "deepseek": {
        "description": "DeepSeek series: Highly efficient MoE models known for coding and mathematical excellence.",
        "description_cn": "DeepSeek 系列：以高效混合专家模型（MoE）著称，在代码和数学领域表现出众。"
    },
    "gemma": {
        "description": "Google Gemma series: Lightweight, state-of-the-art open models built from the same research as Gemini.",
        "description_cn": "Google Gemma 系列：基于 Gemini 研究成果构建的轻量化顶尖开源模型。"
    },
    "glm": {
        "description": "Zhipu GLM series: Frontier Chinese-optimized models with strong multimodal and reasoning capabilities.",
        "description_cn": "智谱 GLM 系列：针对中文深度优化的前沿模型，具备强大的多模态和推理能力。"
    },
    "mistral": {
        "description": "Mistral AI series: Efficient European-developed models featuring sparse MoE architectures.",
        "description_cn": "Mistral AI 系列：欧洲开发的高效模型，采用稀疏混合专家（MoE）架构。"
    },
}


def get_model_metadata(model_id: str, default_desc: str = "") -> Dict[str, str]:
    """
    Get enriched bilingual metadata based on the model ID.
    Uses priority-ordered pattern matching against known model families.
    """
    m_id_lower = model_id.lower()

    # Priority matching
    for pattern, key in _MATCH_RULES:
        if pattern in m_id_lower:
            return MODEL_DATA[key]

    # Broader family fallbacks
    for family, meta in _FAMILY_FALLBACKS.items():
        if family in m_id_lower:
            return meta

    # Absolute fallback
    return {
        "description": default_desc or "No detailed English description available for this model.",
        "description_cn": "暂无该模型的详尽中文技术介绍。"
    }
