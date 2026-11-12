# 远程推理契约（advisor → llm-gateway）

> **方案 B 遗产，方案 C 原样沿用（2026-11-10 平移）。** 本契约继续作为 `new/llm-gateway` 与 `new/advisor`（云内调用）的协议参考；注意现行部署基线见 [new/docs/DEPLOYMENT.md](../new/docs/DEPLOYMENT.md)，开发入口为 [new/README.md](../new/README.md)。

状态：阶段 0 冻结（2026-11-08）。M1 先用网关假模型验证；真实后端按 OpenCode（GLM）优先、Codex 其后的顺序接入。背景见 [plan.md](../plan.md)。

## 1. 总则

- 网关不理解会议语义：`system` 与 `input` 是不透明载荷（仅做大小校验），`metadata` 原样透传。
- 会议上下文、证据与新鲜度判断全部在 advisor 本地完成；网关只负责任务执行与返回。
- 首版按**内部部署无认证**运行（第 2 节）；接入公网/多用户前必须补认证（已列入根 TODO）。

## 2. 传输与认证（首版无认证）

- 传输为 HTTP（localhost 或内网可信环境）；公网部署前必须升级 HTTPS 并启用认证，此前禁止暴露到不可信网络。首版部署基线为当前 laptop、localhost（plan.md 已确认方向第 10 条）。
- 首版**无任何身份认证与用户概念**：任务的幂等、查询与取消均以客户端生成的全局 `request_id` 为准；知道 `request_id` 即可查询或取消该任务。
- 服务端在接口层预留认证中间件插入点（如 `Authorization: Bearer`）；启用后按用户归属校验查询/取消，并增加按用户并发与队列限额（届时更新本契约）。

## 3. 提交任务

`POST /v1/inference-jobs`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | int | 是 | 当前为 `1` |
| `request_id` | str(uuid) | 是 | 客户端生成的稳定身份；同一逻辑任务重试时不变 |
| `content_hash` | str | 是 | 规范化载荷摘要（算法见 8.1） |
| `backend` | str | 是 | `fake` / `opencode`（2026-11-08 起 Codex 取消接入） |
| `model` | str \| null | 否 | 后端内模型选择；缺省用服务器配置 |
| `system` | str | 是 | 角色文本 |
| `input` | object | 是 | 上下文快照，对网关不透明 |
| `output` | str | 是 | 当前仅支持 `decision-v1` |
| `deadline_seconds` | int | 是 | 期望执行期限；服务端上限 120s，超出取上限 |
| `metadata` | object | 否 | ≤4 KiB，原样透传（本地任务关联等），网关不解释 |

响应：

| 状态 | 含义 |
|---|---|
| `202` | 已接收（先持久化再返回），返回任务对象 |
| `200` | 幂等重放：同 `request_id` 且同 `content_hash`，返回既有任务的当前状态 |
| `409 content_conflict` | 同 `request_id` 不同 `content_hash`（任务仍在保留期时可检测） |
| `413 payload_too_large` | `system` + `input` 序列化后超过 2 MiB |
| `422 invalid_request` | 结构或取值不合法 |
| `429 quota_exceeded` | 队列或全局并发已满，附 `retry_after_seconds` |

**重试语义**：同 `request_id` 同 `content_hash` 且任务处于 `failed` / `cancelled` 时，重新提交会重新排队执行；`succeeded` 只返回既有结果，绝不重复执行。

## 4. 任务对象与状态

`GET /v1/inference-jobs/{request_id}` 返回：

```json
{
  "request_id": "…",
  "state": "queued | running | succeeded | failed | cancelled | expired",
  "created_at": "RFC3339",
  "request": {"content_hash": "…", "backend": "opencode", "model": null, "metadata": {}},
  "result": {"output": "decision-v1", "decision": {}, "timings": {"queued_ms": 120, "execution_ms": 8100}},
  "error": {"code": "execution_timeout"}
}
```

- `result` 仅在 `succeeded` 出现；`error` 仅在 `failed` 出现。
- 终态结果保留默认 24 小时，之后状态变为 `expired` 并清除明细；`expired` 后同 id 同 hash 的重新提交按新任务执行。
- 服务端重启：非终态任务标记为 `failed(backend_unavailable)`，客户端按重试语义复用原 `request_id`。

## 5. 取消

`POST /v1/inference-jobs/{request_id}/cancel`

- 幂等：已取消返回 `200`；已 `succeeded` 时返回其终态且不回滚（取消与完成的竞争以真实终态为准）。
- 取消需传播到后端执行进程；清理滞后不得阻塞取消响应。

## 6. 错误码

`invalid_request` / `payload_too_large` / `content_conflict` / `not_found` / `quota_exceeded` / `backend_unavailable` / `execution_timeout` / `output_invalid`（后端输出未通过网关侧结构校验）/ `server_error`。

阶段 4 由 advisor 映射到本地 `ModelError` 语义；诊断不包含载荷正文。

## 7. 资源与调度基线（plan 0.4）

- 全局并发默认 4、队列长度默认 64、deadline 上限 120s、载荷上限 2 MiB、结果保留 24h；均可由服务器配置覆盖。
- 首版无按用户限额（无认证）；补认证后增加每用户并发与队列配额。
- 后端：`fake`（确定性模拟成功、失败、慢响应与取消竞争）与 `opencode`（GLM）。
- OpenCode 的进程启动、隔离目录、工具能力核验与清理全部在服务端完成；对客户端只暴露本契约。

## 8. advisor 侧义务

### 8.1 摘要一致性

`content_hash = sha256(canonical_json({system, input, output, backend, model}))`；canonical JSON 为 sorted keys、`ensure_ascii=False`、紧凑分隔符（与现有 `digest()` 约定一致）。

### 8.2 提交与恢复

- 提交前在本地持久化 `request_id`、`content_hash` 及其与本地任务/会话的关联。
- 网络失败或超时后使用**同一** `request_id` 重试；不得每次生成新身份。

### 8.3 结果应用

- 仅当响应回显的 `content_hash` 与 `metadata` 与本地记录一致时才进入应用；之后照常执行 `Decision.parse`、证据与新鲜度复核及本地幂等提交。
- 会话已结束、角色/材料版本变化或证据失效的迟到结果按既有规则丢弃，不触发任何通知。

## 9. 示例

- **正常**：`POST` → `202 {state:"queued"}` → 轮询 → `succeeded` + `decision`。
- **重复提交**：网络超时后重发同一请求 → `200` 返回当前任务，不重复执行。
- **断开恢复**：laptop 重启 → `GET {request_id}` → 仍在 `running` 或已达终态，按 8.3 应用或按重试语义复用原 id。
- **取消竞争**：`running` 中发出 `cancel` → 若执行已先行完成，返回 `succeeded`，advisor 复核后决定是否应用。
