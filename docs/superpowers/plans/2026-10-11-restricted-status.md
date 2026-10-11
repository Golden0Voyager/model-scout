# 429 语义分类（restricted）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「quota exceeded（计划不可用）」的 429 从「Rate limited（临时限流）」中区分出来，以独立状态 `restricted` 呈现。

**Architecture:** 在 `health_checker._probe_chat_ping` 的 429 分支按错误文案细分；`restricted` 作为普通探测结果走既有 `upsert_health` → API → 前端管线，退役逻辑天然不认它（只认 `offline`），仅加回归测试锁住。前端在两份 `STATUS_META` 各加一条元数据。

**Tech Stack:** Python 3.12 + pytest（backend）、Next.js 16 + React 19 + lucide-react（frontend）。

## Global Constraints

- 包管理器：`uv pip install <pkg>`；运行：`uv run python`（禁止裸 `pip`/`python`）
- 质量门禁：`cd backend && uv run --frozen ruff check . && uv run --frozen mypy . && uv run --frozen pytest --cov tests/ -q`（覆盖率用 `--cov`，`--cov tests/` 会得到 0% 覆盖率并误报）
- 前端门禁：`cd frontend && npm run lint && npx tsc --noEmit`
- 检出内有未提交改动时，**不要使用 `git stash`**（会丢用户工作）；测试隔离用 monkeypatch / 新建实例，不用 stash
- 用户规则：不主动 git commit，全部任务完成后请用户确认再提交
- 提交信息（用户确认后）：conventional commits，中英双语、英文块在前
- 不按 `/models` 列表过滤模型；不修 `_model_ids` 的 `models/` 前缀（设计文档「明确不做」）

---

### Task 1: 后端 429 语义分类

**Files:**
- Modify: `backend/services/health_checker.py:21-27`（ProbeResult 注释）、`:498-504`（429 分支）
- Test: `backend/tests/test_basic.py`（文件末尾追加）

**Interfaces:**
- Consumes: 既有 `_probe_chat_ping(self, model_id: str, provider: ProviderConfig) -> ProbeResult`；`ProbeResult(model_id, provider, status, latency_ms, error_message)`
- Produces: `status="restricted"` + `error_message="Plan quota exceeded (needs billing)"`（quota 语义）；其余 429 保持 `status="error"` + `"Rate limited (429)"`。Task 2 的退役回归测试依赖 `restricted` 这个字面状态值。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_basic.py` 末尾追加。先确认文件头部已有的 import（`SimpleNamespace` 若无则加 `from types import SimpleNamespace`；`get_provider_config` 若无则从 `core.config` 导入；`HealthChecker`、`run`、`pytest`、`Any` 文件里已有）。

```python
# ---------------------------------------------------------------- 429 semantics


QUOTA_429 = (
    "Error code: 429 - [{'error': {'code': 429, 'message': "
    "'You exceeded your current quota, please check your plan and billing details.'}}]"
)
PLAIN_429 = "Error code: 429 - [{'error': {'code': 429, 'message': 'Rate limit reached'}}]"


class _RaisingCompletions:
    """A chat.completions stub that always raises, mimicking a failed ping."""

    def __init__(self, message: str) -> None:
        self._message = message

    async def create(self, **kwargs: Any) -> Any:
        raise Exception(self._message)


class _FakeOpenAI:
    def __init__(self, message: str) -> None:
        self.chat = SimpleNamespace(completions=_RaisingCompletions(message))


@pytest.mark.parametrize(
    ("message", "expected_status", "expected_error"),
    [
        (QUOTA_429, "restricted", "Plan quota exceeded (needs billing)"),
        (PLAIN_429, "error", "Rate limited (429)"),
    ],
)
def test_a_429_is_classified_by_its_semantics(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_status: str,
    expected_error: str,
) -> None:
    """A quota refusal is a plan problem, not a temporary rate limit."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    checker = HealthChecker()
    checker._get_openai_client = lambda provider: _FakeOpenAI(message)  # type: ignore[method-assign]
    provider = get_provider_config("gemini")
    assert provider is not None

    result = run(checker._probe_chat_ping("gemini-2.5-pro", provider))

    assert result.status == expected_status
    assert result.error_message == expected_error
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --frozen pytest tests/test_basic.py -k classified_by_its_semantics -v`
Expected: FAIL——quota 案例得到 `status == "error"`、`error_message == "Rate limited (429)"`（分类尚不存在）

- [ ] **Step 3: 最小实现**

`backend/services/health_checker.py` 的 `_probe_chat_ping` 中，把 429 分支（约 498-504 行）：

```python
            if "429" in msg:
                return ProbeResult(
                    model_id=model_id,
                    provider=provider.key,
                    status="error",
                    error_message="Rate limited (429)",
                )
```

改为：

```python
            if "429" in msg:
                if "quota" in msg.lower():
                    # The model exists; this key's plan cannot call it. Unlike a
                    # rate limit this never recovers on its own — the account
                    # needs billing, not time.
                    return ProbeResult(
                        model_id=model_id,
                        provider=provider.key,
                        status="restricted",
                        error_message="Plan quota exceeded (needs billing)",
                    )
                return ProbeResult(
                    model_id=model_id,
                    provider=provider.key,
                    status="error",
                    error_message="Rate limited (429)",
                )
```

同时把 `ProbeResult` 的 status 注释（约 24 行）：

```python
    status: str  # online | offline | unknown | error | no_key | restricted
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run --frozen pytest tests/test_basic.py -k classified_by_its_semantics -v`
Expected: PASS（2 项参数化全绿）

- [ ] **Step 5: 跑后端全量门禁**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen mypy . && uv run --frozen pytest --cov -q`
Expected: All checks passed / Success / 全绿且覆盖率 ≥ 55%

---

### Task 2: 退役回归测试 + 响应模型注释

**Files:**
- Modify: `backend/core/models.py:28`（HealthStatus 注释）
- Test: `backend/tests/test_basic.py`（紧邻既有退役测试追加，约 980 行 `test_a_model_that_answers_although_unlisted_stays` 之后）

**Interfaces:**
- Consumes: 既有 `_stub_probes(service, statuses, asked)`（按 `provider::model` 键注入探测结果）、`_rescan(service)`、`_service_with_payload(payload, monkeypatch)`、常量 `GONE`（即 `"moonshot::moonshot-v1-8k"`）、`database.get_retirements()`
- Produces: 无新接口——只是行为锁。Task 1 已保证 `restricted` 是合法 status 值。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_basic.py` 既有退役测试组之后追加：

```python
def test_a_restricted_model_is_never_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan refusal proves nothing about the model's existence."""
    service = _service_with_payload({"data": []}, monkeypatch)
    _stub_probes(service, {GONE: "restricted"}, [])

    _rescan(service)

    assert service._retired == set()
    assert run(database.get_retirements()) == {}
```

- [ ] **Step 2: 跑测试确认通过**

Run: `cd backend && uv run --frozen pytest tests/test_basic.py -k restricted_model_is_never -v`
Expected: PASS——`_apply_retirements` 只认 `offline`，这条测试是回归锁而非驱动实现。若 FAIL，说明退役判定被意外改动，回到 Task 1 检查

- [ ] **Step 3: 更新响应模型注释**

`backend/core/models.py:28`：

```python
    status: str = "unknown"  # online | offline | unknown | error | no_key | restricted
```

- [ ] **Step 4: 跑后端全量门禁**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen mypy . && uv run --frozen pytest --cov -q`
Expected: 全绿

---

### Task 3: 前端 STATUS_META

**Files:**
- Modify: `frontend/src/app/page.tsx:1-29`（lucide import）、`:98-105`（STATUS_META）
- Modify: `frontend/src/app/components/ModelModal.tsx:47-53`（STATUS_META）及其 lucide import

**Interfaces:**
- Consumes: `/api/models` payload 中 `health.status === "restricted"`（后端 Task 1 产出）
- Produces: 无。分类过滤（`tabFilter === "online"`）按 `status === "online"` 判断，`restricted` 自动不计入「在线」，无需改过滤逻辑。

- [ ] **Step 1: page.tsx 加 import 与 STATUS_META 条目**

lucide-react import 块（`Activity` 附近）加入 `Ban`。

`STATUS_META`（`page.tsx:98`）追加一条——注意既有 label 全为英文，`restricted` 跟随既有风格，中文说明由 `error_message`（"Plan quota exceeded (needs billing)"）承载（既有渲染在 `h.status !== "online"` 时展示 `error_message`）：

```typescript
  restricted: { label: "Restricted", color: "#d97706", icon: <Ban className="w-3.5 h-3.5" />, sortOrder: 5 },
```

（`#d97706` 为 amber-600，与 `error` 的 `#f59e0b` amber-500 区分开。）

- [ ] **Step 2: ModelModal.tsx 加 import 与同一条目**

`ModelModal.tsx` 的 `STATUS_META`（约 47 行）追加（该文件无 `sortOrder` 字段，图标尺寸为 `w-4 h-4`）：

```typescript
  restricted: { label: "Restricted", color: "#d97706", icon: <Ban className="w-4 h-4" /> },
```

并在其 lucide-react import 中加入 `Ban`（与 `Wifi`、`WifiOff` 等并列）。

- [ ] **Step 3: 跑前端门禁**

Run: `cd frontend && npm run lint && npx tsc --noEmit`
Expected: 无 lint 错误、无类型错误

---

### Task 4: 全量验证与提交

- [ ] **Step 1: 后端全量门禁**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen mypy . && uv run --frozen pytest --cov -q`
Expected: 全绿，覆盖率 ≥ 55%（当前基线约 85%）

- [ ] **Step 2: 前端全量门禁**

Run: `cd frontend && npm run lint && npx tsc --noEmit`
Expected: 全绿

- [ ] **Step 3: 请用户确认后提交**

不自动提交。向用户展示变更摘要（`git diff --stat`），确认后：

```bash
git add backend/services/health_checker.py backend/core/models.py backend/tests/test_basic.py frontend/src/app/page.tsx frontend/src/app/components/ModelModal.tsx
git commit -m "feat: distinguish plan-quota 429 from rate limiting as restricted status

将 quota exceeded 类 429 与临时限流区分，新增 restricted 状态：
Google 免费档 key 调用 Pro 系列返回的 429 是计划/计费问题，
不会随时间恢复，不应显示为临时 Rate limited。"
```
