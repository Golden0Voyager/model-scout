# 429 语义分类：restricted vs Rate limited

日期：2026-10-11
状态：已批准

## 背景与问题

Gemini 免费档 API key 的面板上出现大量「Rate limited (429)」。诊断结论：

- 429 的真实含义是 `You exceeded your current quota`——**模型存在，但当前 key 的计划不可用**（Google 免费档只对 Flash 系列开放，Pro / image / preview 系列一律返回 quota 错误）。
- 这是**计划/计费问题**，不会自己恢复；与临时性 rate limit（时间窗限流，会恢复）性质完全不同。
- 现有代码把所有 429 统一显示为「Rate limited (429)」（`health_checker.py` 的 `_probe_chat_ping`），误导用户以为是临时限流。

诊断中排除的假设（记录以免重查）：

- Gemini 的 `/models` 是**全量模型目录**（62 条，含 `models/` 前缀），**不区分免费/付费**。按 `/models` 列表过滤无法解决 429——面板上 429 的模型恰恰都在列表里。此前「/models 反映 key 可用性」的假设已被实测推翻。
- DeepSeek / Moonshot 的 `/models` 是营销目录（只列新旗舰），同样不可作为「key 可用列表」。按列表过滤会误删实测可用的模型（如 `deepseek-chat`），故不采用任何按列表过滤的方案。
- `_model_ids` 不剥 `models/` 前缀导致探测短路匹配对 Gemini 永远失效——**保持现状**：短路会把「列表内但免费档不可调」的 Pro 模型误报 online，失效反而是对的。

## 目标

- 区分两种 429：quota exceeded（计划不可用）与真限流（临时）。
- 新状态 `restricted` 在面板上有独立、清晰的呈现。
- 不烧额外配额、不引入按 provider 的分支。

## 设计

### 1. 后端：错误语义分类（`backend/services/health_checker.py`）

`_probe_chat_ping` 的 429 分支按错误内容细分：

- 错误信息含 `exceeded your current quota`（不区分大小写；这是计费拒绝的标志性短语——裸 "quota" 会误匹配 Google 可恢复的 RPM/TPM 限流报文）→ `status="restricted"`，`error_message="Plan quota exceeded (needs billing)"`。
- 其余 429 → 保持现状：`status="error"`，`error_message="Rate limited (429)"`。

`restricted` 是探测结果的终态之一，与 `online` / `offline` / `error` / `no_key` / `unknown` 并列。

### 2. 响应模型（`backend/core/models.py`）

`ProbeResult`（及 `/api/scan` 相关响应）的 status 枚举加入 `restricted`。

### 3. 数据流

`ProbeResult(status="restricted")` → `sync_service` 的 `upsert_health` 原样入库（`health_checks.status` 为文本列，无需迁移）→ `GET /api/models` 透传 → 前端渲染。

### 4. 退役判定不受影响

`_apply_retirements` 只认 offline / 模型缺失类证据。`restricted`（及 `error`）不构成退役证据——设计上已满足，加一条回归测试锁住即可。

### 5. 前端（`frontend/app/page.tsx` 的 `STATUS_META`）

新增 `restricted` 条目（`STATUS_META` 在 `page.tsx` 与 `components/ModelModal.tsx` 各有一份，两处都加；本轮不做两份收敛）：

- 标签：「受限」
- 颜色：琥珀色（与现有黄色「异常」区分）
- 说明文案：「模型存在但当前 API key 计划不可用」

分类标签页中不计入「在线」，仅出现在「全部」。

## 明确不做

- **不按 `/models` 列表过滤模型**（见背景：列表语义在 provider 间不一致，会误伤）。
- **不修复 `_model_ids` 的 `models/` 前缀**（见背景：失效反而是对的）。
- **不在本轮隐藏 restricted 模型**（先分类呈现；如需隐藏，后续加前端过滤开关）。
- **已知限制**：付费档 TPM 限流偶尔复用 plan/billing 文案，关键词方案无法 100% 区分，属可接受残留。

## 测试

1. 单测：假 client 抛 429 + `exceeded your current quota` 文案 → `restricted`；429 + 其他文案（含瞬态 RPM 限流报文）→ `error` + "Rate limited (429)"。复用现有 monkeypatch 假 client 模式。
2. 回归：`restricted` 结果不写入 `model_retirements`。
3. 质量门禁：`ruff` / `mypy` / `pytest` 全绿；前端 `npm run lint` + `tsc --noEmit`。
