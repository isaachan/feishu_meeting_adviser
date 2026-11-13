# Advisor 内部 API 契约（bot ↔ advisor）

状态：v1 于 2026-11-10 冻结（阶段 0.3）；**v1.1（2026-11-11）** 新增 NSDP 组织者凭据门（`authorization_required` 增加 `provider`/`submit_url` 变体与 `nsdp_unavailable`；组织者面全部指令前置该门；`GET /sessions/{id}/status` 响应新增 `nsdp_hold`）。**v1.2（2026-11-12）** 新增 §8 GUI 面（`/gui/login/start`·`/gui/login/poll`·`/gui/sessions`·`/gui/logout` + devices 表；经 HTTPS 反代对 laptop GUI 暴露，成为第三个对外入口，见 DEPLOYMENT §2）；**v1.2a（2026-11-13，T7）** NSDP 取数降级语义：`nsdp_unavailable` 不再拒绝入会（见 §1.1）。**v1.3（2026-11-13）** 新增 §9 运维面（admin-site：全量会话列表 + 手工停止）；**v1.4（2026-11-13）** §9.1 响应扩展身份目录与订阅列表（`owner_name`/`owner_email`/`subscriptions[]`，依赖 CONTRACT_AUTH v1.3）。服务端为 `advisor`（会话域 + 一次性操作），客户端为 `bot`（§1–§7）、`macos-gui`（§8）与 `admin-site`（§9）。变更需 bump API 版本并同步两侧实现与测试。上游设计：[ARCHITECTURE](ARCHITECTURE.md) D4/D6、[macos-gui/DESIGN](../macos-gui/DESIGN.md)；outbox 语义沿用旧 advisor（channel=`local`/`im`、uuid 幂等、uncertain 人工裁决）。

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
| `authorization_required` | 组织者凭据缺失/失效。飞书变体附 `authorize_url`；NSDP 变体（v1.1）附 `provider:"nsdp"` + `submit_url`（一次性提交链接，见 CONTRACT_AUTH §8） | 把授权链接/提交链接转发给小A（NSDP 变体用 NSDP 话术） |
| `meeting_not_found` | 日历中无此会议 | 告知原因 |
| `not_organizer` | 小A 非该日历事件组织者 | 告知原因 |
| `outside_window` | 当前不在会议时间窗内 | 告知原因 |
| `auth_unavailable` | 飞书/凭据服务不可达（fail-closed） | 提示稍后重试 |
| `nsdp_unavailable` | NSDP/noam 不可达（**v1.2a 起入会不再触发**：降级建会，见 §1.1；码位保留供后续使用） | 提示"NSDP 服务暂时不可用，稍后再试" |
| `session_ended` / `session_draining` | 会话已结束/排空中，操作被拒 | 告知原因 |
| `not_owner` | 操作者不是该会话/订阅的 owner | 回答"只有组织者可以…" |
| `invalid_state` | 状态机非法迁移（含订阅已 revoked 再申请等） | 按 §6.6 对应话术回复 |
| `internal` | 服务端内部错误 | 提示稍后重试 |

**NSDP 门（v1.1）**：组织者面全部指令（入会/会议列表/状态/结束会议/审批/组织者撤销）在执行前要求该 open_id 已有 NSDP token；无/失效 → 409 `authorization_required`（NSDP 变体），bot 每次都回新提交链接（被动持续提醒）。订阅面（小B 的申请/取消）**不经过**该门。会话进行中 token 失效：监督器置 `nsdp_hold=1`（挂起该会话全部模型决策），经 outbox 向小A 周期性 DM 新提交链接（默认 5 分钟一条）；重新提交成功后自动恢复。NSDP 数据由 advisor 侧取（noam 子进程，token 仅存在于子进程环境），以 `kind:"nsdp"` 材料文档进入模型上下文。**v1.2a（2026-11-13，T7）**：NSDP 取数不可达（超时/后端故障）**不再拒绝入会**——降级为无 NSDP 材料建会，监督器首个维护周期起自动补取（成功即入材料，401 即挂起并提醒）；token 隐式验活由建会时点移至该首次取数。取数侧对同一 token 短 TTL 缓存（默认 10 分钟），避免连续建会重复跑全量 `nsdp info`。

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

## 8. GUI 面（v1.2，macos-gui ↔ advisor）

服务 GUI 客户端（`macos-gui`）的登录与会话发现。与 §1–§7 的差异：**经 TLS 反代对外暴露**（`https://<域名>/gui/*` → `127.0.0.1:8380`，受信网络内 GUI 可达），鉴权用 **device_token**（设备凭据，非用户 OAuth 凭据；身份与登录时序见 CONTRACT_AUTH v1.2 与 [macos-gui/DESIGN](../macos-gui/DESIGN.md) §4）。错误体沿用通用格式；本节专属错误码不进 §1.1 总表。

### 8.1 `POST /gui/login/start` —— 生成扫码登录

```json
{}
```

→ 201：

```json
{"login_url": "https://accounts.feishu.cn/open-apis/authen/v1/authorize?…",
 "device_code": "dc-…", "expires_at": "2026-11-12T15:10:00+08:00"}
```

- advisor 调 auth `POST /authorize/start`（`{"open_id": null, "intent": "gui:login"}`，discovery 变体），`login_url` 即返回的 `authorize_url`；本地生成随机一次性 `device_code`（TTL 10 min）并映射到 auth state。
- auth 不可达 → 409 `auth_unavailable`。

### 8.2 `GET /gui/login/poll?device_code=…` —— 轮询登录结果

```json
{"status": "pending"}
```

完成时：

```json
{"status": "completed", "device_token": "dt-…",
 "user": {"open_id": "ou_A", "name": "Kai Han", "email": "kai.han@nio.com"}}
```

- advisor 内部轮询 auth `GET /authorize/status?state=`；completed 时铸造 `device_token`（`dt-` 前缀 + ≥32B urlsafe 随机），devices 表落 `sha256(token)`、`open_id`、`created_at`、`last_seen_at`，**只返回一次**（重复 poll 同 device_code → 已核销，404）。
- 防冒领：领 token 需同时持有 `device_code`（仅 GUI）与完成 OAuth 的 `state`（仅扫码者链路）。
- 错误：404 `unknown_device`（不存在/已核销）/ 410 `expired`（TTL 过期）。

### 8.3 `GET /gui/sessions` —— 活跃会话发现（Bearer）

请求头 `Authorization: Bearer dt-…` → 200：

```json
{"user": {"open_id": "ou_A", "name": "Kai Han", "email": "kai.han@nio.com"},
 "sessions": [{"session_id": "s-3f9c", "meeting_no": "123456789", "title": "周会",
   "phase": "recording", "started_at": "…",
   "recorder_command_args": {"session": "s-3f9c", "event": "cal_x/evt_y",
     "url": "ws://server:8400/ingest", "token": "st-xxx", "mic": false}}]}
```

- 会话集：该 open_id 名下 `recording`（含完整 `recorder_command_args`，可启动采集）+ `draining`（`recorder_command_args` 为 `null`，仅展示）；按 `started_at` 倒序。args 组装复用建会逻辑（§2），白名单与 CONTRACT_RECORDER_STREAM 附录 A 一致。
- 鉴权：device_token 未知/不匹配 → 401 `bad_token`（GUI 引导重新扫码）。
- 红线：**响应不得含任何用户 OAuth 凭据**；流令牌 `st-xxx` 本就要下发 laptop 内存（与 bot 渲染命令下发同级受信面，v1 接受，见 §8.5）。

### 8.4 `POST /gui/logout` —— 注销设备（Bearer）

→ 200 `{"ok": true}`：删除 devices 表对应行，幂等（不存在的 token 同样 200）。

### 8.5 风险与边界（固化）

| 项 | 评估 | 处置 |
|---|---|---|
| device_token 被盗 = 可领该用户活跃会话的流令牌 | 与 ingest 明文 ws 同级受信面 | v1 受信网络接受；公网前补设备绑定/短 TTL/服务端吊销面 |
| `/gui/sessions` 携流令牌过 HTTPS | 与 bot IM 下发命令同级 | 接受并记录（本表） |
| device_token 无 TTL | 长期有效 | 手动退出/服务端清理即失效；公网前补 |

## 9. 运维面（v1.3，admin-site ↔ advisor）

服务运维工具 `admin-site`（v1 无登录，仅 localhost 受信边界内可达）。与 §1 同端口、同无认证；**不经过 NSDP 门、不校验 owner**（运维动作，非用户动作）。

### 9.1 `GET /admin/sessions` —— 全量会话列表（含 ended 与身份/订阅）

```json
{"sessions": [{"session_id": "s-3f9c", "owner_open_id": "ou_A", "owner_name": "Kai Han",
  "owner_email": "kai.han@nio.com", "meeting_no": "123456789", "title": "周会",
  "phase": "recording", "started_at": 1762800000.0, "ended_at": null, "nsdp_hold": false,
  "subscriptions": [{"request_no": 7, "subscriber_open_id": "ou_B", "state": "approved",
    "subscriber_name": "Bob Li", "subscriber_email": "bob@nio.com"}]}]}
```

- 全部 owner、全部 phase（recording/draining/ended），按 `started_at` 倒序，至多 500 条；`started_at`/`ended_at` 为 Unix 秒（`ended_at` 未结束为 `null`）；`subscriptions` 为该会话全部订阅行（按 request_no 升序，含 requested/approved/denied/revoked 全状态）。
- **身份目录（v1.4）**：`owner_name`/`owner_email`/`subscriber_name`/`subscriber_email` 来自 advisor 身份目录表（`users`）；目录缺失项回源 auth `GET /users/{open_id}/profile`（CONTRACT_AUTH v1.3 §2.4）懒补——授权过的用户即有实名，从未授权者（典型为仅走 IM 的小B）为 `null`，前端回落显示 open_id；auth 不可达时降级为仅 open_id 显示。未知名（null）5 分钟内不重复回源。
- 会话明细复用 §3.2 `/sessions/{id}/status`（该端点本就无 owner 校验）；不新增状态端点。

### 9.2 `POST /admin/sessions/{id}/end` —— 手工停止（进排空）

- 无请求体、无需幂等键（动作天然幂等）；语义与 §3.3 相同（recording→draining，监督器排空后自动 ended）。
- 幂等口径：draining 再调 → 200 `{"phase": "draining"}`；ended 再调 → 200 `{"phase": "ended"}`；不存在 → 404 `not_found`。
- 审计：advisor 记 `admin ending` 日志行（区别于 owner 面的 `session ending`）。

## 10. 版本

API v1（响应体保留 `"api": 1` 字段位，首版不强制）；v1.1 NSDP 门；v1.2 GUI 面（§8）；v1.3 运维面（§9）；v1.4 §9.1 身份目录与订阅列表（依赖 CONTRACT_AUTH v1.3）。不兼容变更递增版本号并同步 bot/advisor/macos-gui/admin-site 侧实现、测试与 `integrations/` 用例。
