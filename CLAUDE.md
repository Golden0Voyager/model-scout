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
- **`api/routes.py`**：API 路由层，`/api/models`、`/api/providers`、`/api/scan` 等。只做校验与转发，扫描编排交给 service
- **`core/config.py`**：静态模型目录 + 供应商配置。含上下文长度、定价、能力标签、双语简介。`default_enabled` 只是**出厂默认**，不是运行时开关
- **`core/models.py`**：Pydantic 响应模型
- **`core/database.py`**：aiosqlite 异步数据库操作，存储健康状态历史与 provider 开关
- **`core/provider_state.py`**：运行时开关的唯一读取入口。`effective_enabled()` 把库里的用户选择叠加在 config 默认之上；库里没有记录的 provider 走默认值
- **`services/health_checker.py`**：健康探测引擎。支持 models_endpoint 探测（免费）和 chat_ping（最小 token 成本），目录缓存 120 秒且失败也缓存
- **`services/sync_service.py`**：同步调度服务。`acquire_scan()` 提供原子扫描槽位，`MIN_SCAN_INTERVAL_SECONDS` 提供冷却，探测为 6 并发 + 150ms 间隔
- **`core/access.py`**：扫描接口的访问控制。`ALLOWED_ORIGINS` 校验请求来源，`probe_limiter` 为单模型探测提供滑动窗口限流
- **`services/fx.py`**：`FxRate` 保存 USD/CNY 参考汇率。由后台任务按 `FX_REFRESH_SECONDS` 刷新，取不到时保留上一次好值、最终回退到 `FALLBACK_CNY_PER_USD`

### 前端（Next.js 16 + React 19 + Tailwind CSS v4）

单页面应用，主要逻辑在 `page.tsx`：
- 分类标签页（全部/在线/免费/对话/视觉/编程/推理/长文本）
- 多维度排序（默认/在线优先/名称/延迟/价格/上下文）
- 卡片/列表双视图切换
- 供应商分组折叠、能力标签筛选、30 秒轮询
- `components/ModelModal.tsx`：模型详情弹窗
- `components/SettingsModal.tsx`：provider 开关面板。读 `GET /api/providers`、写 `PUT /api/providers/{key}`，开关结果由后端整份列表回填
- `lib/format.ts`：`page.tsx` 与弹窗共用的展示层格式化函数（`formatPrice` / `formatContext` / `latencyColor`）。汇率由 `/api/models` 的 `cny_per_usd` 字段下发并逐层传入；导出的 `CNY_TO_USD` 只是后端还没取到汇率时的兜底值

`STATUS_META` 与 `CAPABILITY_LABELS` 目前仍在两个文件里各存一份，尚未收敛。

### 数据库

SQLite `model_scout.db`，aiosqlite 异步操作。表结构在 `core/database.py` 的 `init_db()` 中定义，启动时自动创建：`health_checks`（健康状态）、`scan_history`（扫描历史）、`provider_settings`（用户在设置页拨的开关，`provider_key` 主键 + `enabled` + `updated_at`）。每次操作开短连接，写事务由 `write_lock()` 串行化。

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
- `MODELSCOUT_BACKEND_URL`：前端 rewrite 的代理目标，默认 `http://127.0.0.1:8000`。要在同一台机器上并行跑第二份检出（另一端口）时用它

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
- **汇率走后台**：`FxRate` 由 lifespan 里的独立后台任务刷新（默认 6 小时），**不在读接口里惰性拉取**——否则会把上面那条不变式重新打开。lifespan 也不 `await` 首次刷新，否则两个源各 10 秒超时会把启动卡住最多 20 秒；这期间 payload 下发兜底值。返回给前端的是 `cny_per_usd`，超出 1–20 合理区间的值一律拒用
- **写接口来源校验**：三个 `POST /api/scan*` 与 `PUT /api/providers/{key}` 走 `require_trusted_origin`（`GET /api/providers` 不设守卫，它不写任何东西）。浏览器对跨源写请求一定附带 `Origin`，因此不在白名单（含 `Origin: null`）即 403，无需向前端分发密钥。Next 的 rewrites 是服务端代理且会透传 `Origin`，所以前端链路同样受保护、也照常放行（已实测）。**不带 `Origin` 视为本地请求**：curl 和本地脚本本来就能读到 `.env` 里的全部密钥，给它们加令牌保护不了任何东西。若将来把服务绑到非回环地址，这套就不够了，需要真正的鉴权
- **面板换端口时**：`ALLOWED_ORIGINS` 与 CORS 共用同一份常量，改端口要更新 `core/access.py`；前端目标地址由 `MODELSCOUT_BACKEND_URL` 给出，不必再改 `next.config.ts`
- **单模型探测限流**：`POST /api/scan/{provider}/{model}` 每次都是一笔真实请求且不受扫描冷却约束，故独立限流为 30 次/60 秒，超限返回 429 并带 `Retry-After`
- **模型缓存**：同一供应商的 models 列表在 30s TTL 内只请求一次，缓存在 `HealthChecker._models_cache`
- **后台刷新**：启动时自动全量扫描，之后每 5 分钟（`SCAN_INTERVAL_MINUTES`）后台自动刷新
- **状态颜色**：在线(绿) / 离线(红) / 异常(黄) / 未配置(灰) / 未知(蓝)
- **provider 开关以设置页为准**：运行时状态存在 SQLite 的 `provider_settings` 表，由设置页（`PUT /api/providers/{key}`）唯一控制；`ProviderConfig.default_enabled` 只是**首次启动的默认值**，改它不会覆盖库里已有的用户选择。读取一律经 `core/provider_state.effective_enabled()`，不要再去 import config 的默认值
- **关闭 = 隐藏**：被关掉的 provider 不出现在 `GET /api/models` 的模型列表、`providers` 汇总和 `total_models` 里，也不参与任何发现与探测；但 `GET /api/providers` **一定列出全部** provider（含关闭的），否则用户无法在设置页把它打开——这条有回归测试守着
- **打开时会补扫**：`set_provider_enabled(True)` 触发一次全量扫描。纯发现型 provider 打开后还没有任何已知模型，只有全量扫描能填回来；定向扫描不够
- **`probe_mode="none"`**：仍然支持的**模型级**开关，但目前没有任何静态条目使用它——三个坏掉的 provider 都改用上面的运行时开关
- **被关掉的 provider 无法定向扫描**：`POST /api/scan/{provider}` 与单模型扫描会返回 409，提示去设置页打开
