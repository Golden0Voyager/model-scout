## ⚠️ 环境约束（强制）

- **包管理器**：`uv pip install <pkg>`（禁止 `pip` / `python -m pip`）
- **运行脚本**：`uv run python <script>.py`（禁止直接 `python`）

---

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

- **`app.py`**：FastAPI 入口，lifespan 管理后台定时扫描任务，并解析出站代理设置
- **`api/routes.py`**：API 路由层，`/api/models`、`/api/scan` 等。只做校验与转发，扫描编排交给 service
- **`core/config.py`**：静态模型目录 + 供应商配置。含上下文长度、定价、能力标签、双语简介
- **`core/models.py`**：Pydantic 响应模型
- **`core/database.py`**：aiosqlite 异步数据库操作，存储健康状态历史
- **`services/health_checker.py`**：健康探测引擎。支持 models_endpoint 探测（免费）和 chat_ping（最小 token 成本），带 30s 缓存
- **`services/sync_service.py`**：同步调度服务。`acquire_scan()` 提供原子扫描槽位，`MIN_SCAN_INTERVAL_SECONDS` 提供冷却，探测为 6 并发 + 150ms 间隔

### 前端（Next.js 16 + React 19 + Tailwind CSS v4）

单页面应用，主要逻辑在 `page.tsx`：
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

复制 `backend/.env.example` 为 `backend/.env` 并填入 API Key。`tests/test_basic.py` 会校验每个供应商的 `api_key_env` 在 `.env.example` 中有对应条目，新增供应商时需同步维护。

- `MODELSCOUT_PROXY_URL`：仅供 `network="proxy"` 的供应商使用；未设置时回退到 `https_proxy`/`http_proxy`
- `SCAN_INTERVAL_MINUTES`：后台扫描周期，默认 5

## 质量门禁

```bash
cd backend && uv run --frozen ruff check . && uv run --frozen mypy . && uv run --frozen pytest --cov tests/
cd frontend && npm run lint && npx tsc --noEmit
```

- `backend/uv.lock` 必须包含 dev 依赖组。改完 `pyproject.toml` 要重新 `uv lock`，否则 CI 的 `uv sync --dev --locked` 报错
- 覆盖率门槛在 `[tool.coverage.report] fail_under`，按当前实际水平设定，只升不降

## 开发注意事项

- **代理路由**：`core/config.py` 的 `network` 字段决定走向——`proxy` 走 `MODELSCOUT_PROXY_URL`（海外供应商），`direct` 始终绕过代理（国内供应商）。两个 httpx 客户端都带 `trust_env=False`，防止环境里的 `*_proxy` 把 direct 组也卷进海外出口，不要移除
- **只读接口不碰外网**：`GET /api/models` 只读 SQLite；模型发现仅由启动扫描、定时任务和显式 `POST /api/scan` 触发
- **模型缓存**：同一供应商的 models 列表在 30s TTL 内只请求一次，缓存在 `HealthChecker._models_cache`
- **后台刷新**：启动时自动全量扫描，之后每 5 分钟（`SCAN_INTERVAL_MINUTES`）后台自动刷新
- **状态颜色**：在线(绿) / 离线(红) / 异常(黄) / 未配置(灰) / 未知(蓝)
- **AnyRouter**：上游端点当前不可达，`probe_mode="none"` 让它只作目录展示、不参与探测；端点恢复后删掉该字段即可
