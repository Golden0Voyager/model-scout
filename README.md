# ModelScout (LLM 免费模型侦察兵)

一个用于追踪和衡量主流模型平台（OpenRouter, Groq, DashScope）免费模型性能的现代化监控大屏。

## 🚀 核心功能
- **自动化追踪**: 实时从 OpenRouter API 抓取 `pricing: 0` 的最新模型。
- **性能基准测试**: 测量首字延迟 (TTFT) 和 生成速度 (TPS)。
- **高颜值大屏**: Next.js 开发的深色系、玻璃拟态监控面板。
- **多平台支持**: 已完成 OpenRouter, Groq, DashScope (阿里云) 的接入。

## 🛠️ 技术栈
- **后端**: Python 3.12+, FastAPI, `uv` (包管理), OpenAI SDK (异步流式)。
- **前端**: Next.js 14, Tailwind CSS, Lucide Icons, Framer Motion.

## 🏃 快速启动
1. **配置环境**: 确保根目录的 `backend/.env` 中包含你的 API Keys。
2. **运行脚本**:
   ```bash
   chmod +x start.sh
   ./start.sh
   ```
3. **访问大屏**: 打开浏览器访问 `http://localhost:3000`。

---
*Created by Antigravity AI for Haining's Code Lab.*
