# Advisor 内部 API 契约（bot ↔ advisor）

状态：v1 于 2026-11-10 冻结（阶段 0.3）。服务端为 `new/advisor`（会话域 + 一次性操作），客户端为 `new/bot`。变更需 bump API 版本并同步两侧实现与测试。上游设计：[ARCHITECTURE](ARCHITECTURE.md) D4/D6；outbox 语义沿用旧 advisor（channel=`local`/`im`、uuid 幂等、uncertain 人工裁决）。

## 1. 传输与通用约定

- HTTP/1.1 JSON，`http://127.0.0.1:8380`，仅 localhost（v1 无认证，受信边界内；拆机部署时补服务间认证）。
- 所有 POST 必须携带 `X-Idempotency-Key`（bot 以 `uuid5(NAMESPACE_URL, message_id + ":" + action)` 派生）：同 key 同请求体 → 重放原响应，不重复执行（语义沿用 llm-gateway `request_id`）。
- 请求者身份以字段显式携带（`owner_open_id` / `subscriber_open_id`，bot 从 IM 事件获得），v1 信任 bot。
- 通用错误体：`{"error": {"code": "...", "message": "..."}}`；HTTP 状态仅作粗分类（400 bad_message / 404 not_found / 409 state / 500 internal）。

### 1.1 错误码总表

| code | 场景 | bot 行为 |
|---|---|---|
| `bad_message` | 字段非法/缺失 | 回用户"指令格式有误" |
| `not_found` | 会话/订阅请求不存在；或订阅目标会议未命中 | 回用户"未找到该会议或会议已结束" |
| `authorization_required` | 组织者凭据缺失/失效（响应体附 `authorize_url`） | 把授权链接转发给小A |
| `meeting_not_found` | 日历中无此会议 | 告知原因 |
| `not_organizer` | 小A 非该日历事件组织者 | 告知原因 |
| `outside_window` | 当前不在会议时间窗内 | 告知原因 |
| `auth_unavailable` | 飞书/凭据服务不可达（fail-closed） | 提示稍后重试 |
| `session_ended` / `session_draining` | 会话已结束/排空中，操作被拒 | 告知原因 |
| `not_owner` | 操作者不是该会话/订阅的 owner | 告知"只有组织者可以…" |
| `invalid_state` | 状态机非法迁移（含订阅已 revoked 再申请等） | 按 §6.6 对应话术回复 |
| `internal` | 服务端内部错误 | 提示稍后重试 |

## 2. `POST /sessions` —— 入会（S1）

```json
{"owner_open_id": "ou_A", "meeting_ref": {"no": "123456789", "password": ""},
 "mic_enabled": false}
```

成功（201）：

```json
{"session_id": "s-3f9c", "meeting_no": "123456789", "title": "周会",
 "phase": "recording", "started_at": "2026-11-10T14:00:00+08:00",
 "recorder_command_args": {"session": "s-3f9c", "event": "cal_x/evt_y",
   "url": "ws://server:8400/ingest", "token": "st-xxx", "mic": false}}
```

- 语义：校验（会议存在 + 小A 是组织者 + 时间窗）→ 建会（材料 = 日历事件描述，空则无）→ 铸流令牌 → 监督器自动 consume。命令文本由 bot 按模板渲染（附录见 CONTRACT_RECORDER_STREAM）；**响应中不得含任何用户凭据**。
- 凭据缺失/失效（409）：

```json
{"error": {"code": "authorization_required", "message": "user token missing/expired"},
 "authorize_url": "https://<域名>/oauth/start?state=…"}
```

  小A 完成授权后重发入会指令（state 已绑定意图，auth 侧可提示续接）。
- 四种拒绝：`authorization_required` / `meeting_not_found` / `not_organizer` / `outside_window`（S1 验收口径逐条对应）。
- 幂等与并发：同一 `(owner, meeting_no)` 已有 recording 会话 → 返回既有会话（不重建、令牌不换）；同 key 重放 → 原响应。

## 3. 会话查询与结束

### 3.0 `GET /meetings?owner_open_id=ou_A` —— `会议列表` 指令的数据源（T1）

```json
{"now": 1762800000, "meetings": [{"meeting_no": "123456789", "title": "周会",
  "start_ts": 1762801200, "end_ts": 1762804800,
  "is_organizer": true, "in_progress": false}]}
```

- 时间窗：[now−30min, now+7d]，按 start 升序，至多 10 条；`meeting_no` 取自事件 `vchat.meeting_url`，无视频链接的事件为 `null`。
- 凭据缺失/失效 → 409 `authorization_required`（附 `authorize_url`，intent=`list:meetings`），与入会同门。
- 只读，不建会话、不进模型上下文。

### 3.1 `GET /sessions?owner_open_id=ou_A` —— `状态` 指令的数据源

```json
{"sessions": [{"session_id": "s-3f9c", "meeting_no": "123456789", "title": "周会",
  "phase": "recording", "started_at": "…", "idle_timeout_s": 900}]}
```

只返回 recording/draining 会话（按 started_at 倒序；"最近一场 recording" = 首元素）。

### 3.2 `GET /sessions/{id}/status?owner_open_id=ou_A`

```json
{"session_id": "s-3f9c", "phase": "recording", "started_at": "…",
 "sources": [{"source": "loopback", "connected": true, "acked_sequence": 5183, "drained": false},
             {"source": "mic", "connected": false, "acked_sequence": 0, "drained": false}],
 "model": {"pending_jobs": 1, "inflight_jobs": 1, "decisions": 12},
 "subscriptions": [{"request_no": 7, "subscriber_open_id": "ou_B", "state": "approved"}],
 "material_present": true}
```

### 3.3 `POST /sessions/{id}/end` —— `结束会议`（S5）

```json
{"owner_open_id": "ou_A"}
```

成功（200）：`{"session_id": "s-3f9c", "phase": "draining"}`。会话进入 draining：在途输入/模型任务/待发回答全部落定后自动置 ended（draining 排空语义沿用旧 advisor）。已 ended 再调 → 幂等返回 `{"phase": "ended"}`。非 owner → `not_owner`。

## 4. 订阅申请（S3.1–S3.2）

### 4.1 `POST /subscription-requests`

```json
{"subscriber_open_id": "ou_B", "meeting_no": "123456789"}
```

成功（201）：

```json
{"request_no": 7, "session_id": "s-3f9c", "state": "requested",
 "owner_open_id": "ou_A", "title": "周会"}
```

- 会议号解析到**任何 owner** 名下 phase=recording 的会话才受理；未命中 / draining / ended → `not_found`（bot 回"未找到该会议或会议已结束"）。
- `request_no`（即 #rN）**全局唯一**、单调递增，advisor 分配。
- 审批请求的 IM 投递由 bot 直接完成（不走 outbox，不承诺可靠投递——bot 离线期间申请可丢，小B 重发即可）。

## 5. 审批与撤销（S3.3–S3.5）

### 5.1 `POST /subscription-requests/{request_no}/decide`

```json
{"owner_open_id": "ou_A", "decision": "approve" | "deny"}
```

成功（200）：`{"request_no": 7, "state": "approved" | "denied"}`。

- 仅 `requested` 可 decide；重复 decide 幂等返回当前状态；非该会话 owner → `not_owner`；请求已撤销 → `invalid_state`。
- `approve` 生效口径：**从下一条决策起**该收件人进入 fan-out 集合（仅前向，不回溯）。

### 5.2 `POST /subscriptions/{request_no}/revoke`

```json
{"owner_open_id": "ou_A"}
```

成功（200）：`{"request_no": 7, "state": "revoked"}`。

- 调用者必须是**会话 owner 或该订阅的 subscriber 本人**（二选一字段：`owner_open_id` / `subscriber_open_id`；2026-11-11 T1 修订：参会者自退订）。
- 仅 `approved` 可撤销（`requested`/`denied` 下撤销 → `invalid_state`）；**revoked 为终态**。
- 撤销即刻止血：该收件人 pending 的 IM 行置 cancelled（沿用旧 `reconcile_destinations` 语义），后续决策不再 fan-out。

## 6. 与 REQUIREMENTS S3 边界的逐条对应

| S3 边界 | 契约落点 |
|---|---|
| 仅前向，不回溯 | §5.1 approve 只影响后续决策的 fan-out 集合 |
| 小B 只收回答，非字幕/录音 | outbox 行仅 `kind=reply` 的 IM 投递（§7） |
| 小B 消息不进模型上下文 | 订阅面无任何写会话上下文的资源 |
| 审批请求不承诺可靠投递 | §4.1 bot 直发，不走 outbox |
| 已批准分发走 outbox，可靠 | §7 pending/result 状态机 |
| approved 再申请幂等 | §6.1 |
| denied 再申请走新审批 | §6.2 |
| revoked 终态，再申请拒绝 | §6.3 |
| ended 拒新订阅 | §4.1 draining/ended → not_found |

### 6.1 approved 再申请（幂等提示，不新建）

```json
{"subscriber_open_id": "ou_B", "meeting_no": "123456789"}
→ 200 {"request_no": 7, "state": "approved", "existing": true}
```

### 6.2 denied 再申请（新建审批）

```json
→ 201 {"request_no": 12, "state": "requested", "previous": {"request_no": 7, "state": "denied"}}
```

### 6.3 revoked 再申请（终态拒绝）

```json
→ 409 {"error": {"code": "invalid_state",
         "message": "subscription #7 revoked by organizer; terminal state"}}
```

bot 回小B："该订阅已被组织者撤销"。

## 7. outbox 拉取与回写（通知面）

### 7.0 `GET /subscriptions?subscriber_open_id=ou_B` —— 参会者查询自己的订阅

```json
{"subscriptions": [{"request_no": 7, "session_id": "s-3f9c", "meeting_no": "123456789",
  "title": "周会", "state": "approved"}]}
```

（T1 新增：供 bot 的「取消订阅 [会议号]」指令定位订阅。）

### 7.1 `GET /outbox/pending?limit=10&owner_open_id=ou_A`

```json
{"rows": [{"uuid": "u-1a2b", "kind": "reply", "channel": "im",
  "recipient_open_id": "ou_A", "session_id": "s-3f9c",
  "payload": {"text": "建议补充预算数据…"}, "created_at": "…", "attempts": 0}]}
```

- 只返回 `channel=im` 且 `state=pending` 的行（`local` 行是决策事实来源，不经传输）；按 created_at 升序、limit 上限 50。
- `owner_open_id` 可选：缺省返回全部 owner 的待发行（bot 单实例拉全量）。

### 7.2 `POST /outbox/{uuid}/result`

```json
{"status": "sent" | "uncertain", "error": "…", "message_id": "om_…"}
```

- `sent`：pending→sent；`uncertain`（超时/未知结果）：pending→uncertain，人工裁决后经运维面终定（沿用旧语义）；非 pending 状态收到重复结果 → 幂等忽略（200）。
- 发送侧幂等：bot 用 `uuid5(NAMESPACE_URL, outbox_uuid)` 作为飞书回复 uuid，重复发送被飞书去重。

## 8. 版本

API v1（响应体保留 `"api": 1` 字段位，首版不强制）。不兼容变更递增版本号并同步 bot/advisor 两侧实现、测试与 `new/integrations/` 用例。
