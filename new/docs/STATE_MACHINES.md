# 状态机规格 · 订阅与会话（阶段 0.6 冻结）

本文是 advisor 实现与测试的对照规格（带迁移约束与错误码），语义冻结自 [REQUIREMENTS](REQUIREMENTS.md) S3/S5 与 [ARCHITECTURE](ARCHITECTURE.md) 4.2/4.3。API 错误码见 [CONTRACT_ADVISOR_API](CONTRACT_ADVISOR_API.md) §1.1；ingest 侧错误码见 [CONTRACT_RECORDER_STREAM](CONTRACT_RECORDER_STREAM.md) §3.4。变更需同步本文与两侧契约。

## 1. 订阅状态机

状态集：`requested` / `approved` / `denied` / `revoked`。**`revoked` 为终态。**

### 1.1 合法迁移

| # | 从 | 迁移 | 触发 | 约束 | API 结果 |
|---|---|---|---|---|---|
| T1 | — | → `requested` | `POST /subscription-requests`（小B 申请） | 目标会话 phase=`recording`；该 `(subscriber, session)` 无 `approved` 行；无 `revoked` 行 | `201 {request_no, state:requested}` |
| T2 | `requested` | → `approved` | owner `decide=approve` | 决策者 = 会话 owner；即刻进入 fan-out 集合（仅前向） | `200 {state:approved}` |
| T3 | `requested` | → `denied` | owner `decide=deny` | 同上 | `200 {state:denied}` |
| T4 | `approved` | → `revoked` | owner `revoke`；或 **subscriber 自退订**（T1 修订：调用者为该订阅 subscriber） | 终态；同时 cancel 该收件人 pending IM 行 | `200 {state:revoked}` |
| T5 | `denied` | → `requested`（新行） | 小B 再申请 | 新 `request_no`，旧行保留 `denied` | `201 {request_no 新, previous}` |

### 1.2 幂等与拒绝（非迁移响应）

| 情形 | 响应 |
|---|---|
| `approved` 再申请（T1 重复触发） | `200 {state:approved, existing:true}`，不新建 |
| 对非 `requested` 重复 `decide`（同义或异义） | `200 {当前状态}`（幂等回显） |
| 已 `revoked` 的申请再 `decide` | `409 invalid_state` |
| `revoked` 后小B 再申请（T1） | `409 invalid_state`（bot 话术："该订阅已被组织者撤销"） |
| `requested`/`denied` 上 `revoke` | `409 invalid_state` |
| 目标会话 `draining`/`ended` 时申请（T1） | `404 not_found`（"未找到该会议或会议已结束"） |
| 非 owner 的 `decide`/`revoke` | `409 not_owner` |

### 1.3 不变量

- 仅 `approved` 收分发；fan-out 集合在**每次决策时**实时查询（审批即刻生效、撤销即刻止血）。
- 仅前向：`approve` 只影响其后产生的决策，不回溯、不补发。
- 订阅行不随会话 ended 自动迁移（会话结束后集合不再被查询）。

## 2. 会话状态机

状态集：`recording` / `draining` / `ended`。**无 `paused`**（v1 无暂停辅助）。

### 2.1 合法迁移

| # | 从 | 迁移 | 触发 | 约束 |
|---|---|---|---|---|
| S1 | — | → `recording` | `POST /sessions` 成功 | 校验三通过 + 建会 + 铸令牌 + 监督器启动 consume |
| S2 | `recording` | → `draining` | owner `POST /sessions/{id}/end`；**或**采集空闲超时 | 空闲 = 无活跃 ingest 连接且无新帧持续 `idle_timeout_s`（config，默认 900） |
| S3 | `draining` | → `ended` | 排空条件全部满足（见 2.2） | 监督器自动执行；无人工 CLI |
| S4 | `recording` | → `ended` | `end` 时已满足排空条件 | S2+S3 的瞬时合并（沿用旧 `('recording','ended')` 合法迁移） |

### 2.2 排空条件（S3）

1. 每个已声明源（曾 `start` 过的 `loopback`/`mic`）满足其一：`stream_end` 确认到 `last_sequence`（drained）；或存在显式丢失区间且其后无再连接（lost）；或从未连接且会话已过空闲窗（abandoned，等价 lost）。
2. 无 in-flight 模型任务（gateway 任务终态或过期）。
3. outbox 无 `pending` 行（`sent`/`cancelled` 达终态；`uncertain` 不阻塞 ended，留人工裁决面）。

### 2.3 非法迁移与拒绝

| 情形 | 错误码 |
|---|---|
| `ended` 后任何迁移/写操作 | `409 session_ended`（API） |
| `draining` 后 `POST /sessions` 同会话复建 | 新建独立判断（旧会话不可复活；同会议重入会=等 ended 后新会话） |
| `draining`/`ended` 时新 ingest 连接 | `session_ended`（CONTRACT_RECORDER_STREAM §3.4；draining 期在途连接由服务端冲刷后关闭） |
| `draining`/`ended` 时订阅申请 | `404 not_found`（§1.2） |
| 重复 `end` | 幂等：`200 {phase: 当前}` |

### 2.4 不变量

- `ended`：拒新订阅、拒新连接、迟到模型结果不应用（旧语义）。
- 迁移持久化先行（先写库再对外可见）；监督器崩溃重启后恢复未 ended 会话的循环（plan 1.4）。
- 流令牌随 `ended` 即刻吊销；`draining` 期在途令牌保持有效至排空或会话 ended。

## 3. 源（ingest 流）状态（供排空判定引用）

| 状态 | 含义 | 终态 |
|---|---|---|
| `live` | 有活跃连接（最近 `started` 未关闭） | 否 |
| `drained` | `stream_end` 已确认到 `last_sequence` | 是 |
| `lost` | 存在显式丢失区间且其后无重连，或会话 ended 时仍 `live` | 是 |

源状态不独立对外暴露（`GET /sessions/{id}/status` 的 `sources[]` 为只读投影）。
