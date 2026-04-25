# CLAUDE.md — ModelScout v2.0

## 项目概述

ModelScout v2.0 是一个模型可用性监控面板，追踪多供应商 LLM 的在线状态、延迟、定价与能力标签。设计灵感来自 OpenRouter 模型目录。

## 运行命令

```bash
# 一键启动前后端（后端 8000，前端 3000）
./start.sh

# 或手动启动
# 后端
cd backend
uv run python app.py

# 前端
cd frontend
npm run dev
```

访问 http://localhost:3000

## 架构

### 后端（FastAPI + SQLite）

- **`app.py`**（124 行）：FastAPI 入口， lifespan 管理后台定时扫描任务
- **`api/routes.py`**（70 行）：API 路由层，`/api/models`、`/api/scan` 等
- **`core/config.py`**（651 行）：静态模型目录 + 供应商配置。30+ 模型，含上下文长度、定价、能力标签、双语简介
- **`core/models.py`**（65 行）：Pydantic 响应模型
- **`core/database.py`**（130 行）：aiosqlite 异步数据库操作，存储健康状态历史
- **`services/health_checker.py`**（346 行）：健康探测引擎。支持 models_endpoint 探测（免费）和 chat_ping（最小 token 成本），带 30s 缓存
- **`services/sync_service.py`**（337 行）：同步调度服务，8 并发 + 150ms 间隔，控制探测节奏

### 前端（Next.js 16 + React 19 + Tailwind CSS v4）

单页面应用，全部逻辑在 `page.tsx`（1140 行）：
- 分类标签页（全部/在线/免费/对话/视觉/编程/推理/长文本）
- 多维度排序（默认/在线优先/名称/延迟/价格/上下文）
- 卡片/列表双视图切换
- 供应商分组折叠、能力标签筛选、30 秒轮询
- `components/ModelModal.tsx`：模型详情弹窗

### 数据库

SQLite `model_scout.db`，aiosqlite 异步操作。表结构在 `core/database.py` 的 `init_db()` 中定义，启动时自动创建。

## 探测策略

| 方式 | 说明 | 成本 |
|---|---|---|
| `models_endpoint` | HEAD/GET 供应商 `/models` 接口 | 免费 |
| `chat_ping` | 发送 `max_tokens=1` 的极简请求 | 极低 |
| `none` | 跳过探测，仅展示信息 | 无 |

## 环境变量

复制 `backend/.env.example` 为 `backend/.env`，填入对应 API Key：
`SCNET_API_KEY`、`AIHUBMIX_API_KEY`、`OPENROUTER_API_KEY`、`GROQ_API_KEY`、`DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY`

## 开发注意事项

- **代理**：`start.sh` 已注入 Clash Verge 代理；`config.py` 中 `network="direct"` 的供应商（如 SCNet）会绕过代理
- **模型缓存**：同一供应商的 models 列表在单次扫描中只请求一次，缓存在 `HealthChecker._models_cache`
- **后台刷新**：启动时自动全量扫描，之后每 5 分钟（`SCAN_INTERVAL_MINUTES`）后台自动刷新
- **状态颜色**：在线(绿) / 离线(红) / 异常(黄) / 未配置(灰) / 未知(蓝)
- **AnyRouter**：当前 `probe_mode=none`，仅作为占位符展示模型信息
