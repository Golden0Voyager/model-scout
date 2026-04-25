# ModelScout v2.0

模型可用性监控面板。追踪你拥有 API Key 或免费额度的所有供应商模型，显示在线状态、延迟、模型 ID、所属供应商以及模型基本情况信息（简介、上下文、能力标签、定价等）。

设计灵感来自 [OpenRouter](https://openrouter.ai/) 的模型目录和排行榜。

## 快速启动

```bash
# 1. 确保 API Key 已配置
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入你的 API Key

# 2. 一键启动前后端
./start.sh
```

访问 http://localhost:3000

## v2.0 改进

### 1. 配置驱动模型库
不再依赖各平台不稳定的 models API 动态抓取。文档中提到的 **SCNet、DeepSeek、Moonshot、AnyRouter、AIHubMix** 等所有模型已作为静态配置写入 `backend/core/config.py`，包含：
- 准确的上下文长度、最大输出长度
- 输入/输出定价（¥/百万 tokens）
- 能力标签（chat / coding / reasoning / vision / function_calling / long_context / MoE）
- 中英双语简介

### 2. 轻量级健康探测
替代了原先沉重的完整 Chat Completion Benchmark：
- **models 端点探测**：优先调用各平台的 `/models` 接口（免费、快速、不消耗 token）
- **最小化 chat ping**：若 models 接口不可用，发送 `max_tokens=1` 的极简请求验证实际推理能力
- **智能缓存**：同一供应商的 models 列表在单次批量扫描中只请求一次，避免重复 HTTP 调用
- **并发控制**：8 并发 + 150ms 间隔，polite to APIs

### 3. SQLite 持久化
健康状态存储在 `backend/model_scout.db`，重启不丢失。

### 4. 后台定时刷新
启动时自动扫描一次，之后每 5 分钟后台自动刷新，无需手动触发。

### 5. 借鉴 OpenRouter 的前端设计

#### 分类标签页
顶部快速切换不同视角，每个标签带实时计数：
- **全部** · **在线** · **免费** · **对话** · **视觉** · **编程** · **推理** · **长文本**

#### 多维度排序
下拉菜单支持：
- 默认排序（按供应商 + 在线优先 + 延迟）
- 在线优先
- 名称
- 延迟（低→高）
- 价格（低→高）
- 上下文长度（高→低）

#### 双视图切换
- **卡片视图**：信息完整，适合浏览详情
- **列表视图**：紧凑高效，适合快速对比

#### 交互细节
- 悬停显示「复制模型ID」按钮
- 免费模型带绿色徽章
- 结果计数器（显示 xx/xx 个模型）
- 按供应商分组，支持折叠/展开
- 能力标签快速筛选
- 30 秒轮询（标签页可见时）

### 6. 状态系统

| 状态 | 颜色 | 含义 |
|---|---|---|
| 在线 | 绿色 | 模型可用，延迟正常 |
| 离线 | 红色 | 模型在提供商端不可用 |
| 异常 | 黄色 | 请求出错（限流、网络等） |
| 未配置 | 灰色 | 缺少 API Key |
| 未知 | 蓝色 | 跳过探测（如 AnyRouter 占位符） |

## 项目结构

```
model_scout/
├── backend/
│   ├── app.py              # FastAPI 入口 + 后台定时扫描
│   ├── api/routes.py       # API 路由
│   ├── core/
│   │   ├── config.py       # 供应商配置 + 30 模型静态目录
│   │   ├── models.py       # Pydantic 数据模型
│   │   └── database.py     # SQLite 异步操作
│   └── services/
│       ├── health_checker.py  # 健康探测引擎（含 models 缓存）
│       └── sync_service.py    # 同步调度服务
├── frontend/
│   └── src/app/
│       ├── page.tsx        # 主面板（卡片+列表双视图）
│       └── globals.css     # 主题样式
├── start.sh                # 一键启动脚本
└── verify_project.sh       # 项目验证脚本
```

## 支持的供应商

| 供应商 | 探测方式 | 状态 |
|---|---|---|
| SCNet | models API + chat ping | 动态探测 |
| AIHubMix | models API + chat ping | 动态探测 |
| DeepSeek | chat ping | 动态探测 |
| Moonshot | chat ping | 动态探测 |
| AnyRouter | 跳过（待配置 endpoint） | 仅展示信息 |

> **注意**：AnyRouter 当前配置 `probe_mode=none`，仅作为占位符展示模型信息。如需启用探测，请在 `backend/core/config.py` 中补充 endpoint 和 API key 配置。

## API 端点

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | 服务健康检查 |
| GET | `/api/models` | 获取所有模型 + 健康状态 |
| POST | `/api/scan` | 手动触发全量扫描 |
| POST | `/api/scan/{provider}` | 刷新单个 Provider 的所有模型 |
| POST | `/api/scan/{provider}/{model_id}` | 刷新单个模型 |
| GET | `/api/models/{model_id}` | 获取单个模型详情 |

## 环境要求

- Python 3.12+
- Node.js 18+
- uv (Python 包管理)
- npm
