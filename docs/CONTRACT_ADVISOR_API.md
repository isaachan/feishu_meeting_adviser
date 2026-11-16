# Advisor 内部 API 契约（bot ↔ advisor）

> **v2.0（2026-11-16，SM-01）**：状态机 v2 落契约——会话 phase `recording→running` 更名并新增 `paused`（语义见 [STATE_MACHINES](STATE_MACHINES.md) v2）；会话按**会议实例唯一**（`meeting_instance_key`），新增单人单占位；订阅新增 `expired`/`subscriber_cancelled`/`owner_revoked`、`requested` 可撤回、`owner_revoked` 后可重申；新增 §9.3/§9.4 supervisor（小C）名单配置面；§1.1 新增错误码；§8.3 GUI 面新增 `paused` 展示。不兼容变更（phase 更名、订阅语义）， advisor/bot/admin-site/macos-gui 同仓同步切换（§10）。
>
> **v1.6（2026-11-14）**：`POST /sessions` 入会前检查组织者飞书 OAuth、NSDP token、Jira PAT；缺失时一次返回所有缺失项及链接，bot 转发组织者，完成后重发入会。仅入会设此门，NSDP/Jira 在会中查询失败不暂停其他判断；不在建会时预取数据。飞书 token 可复用 GUI 授权；NSDP/Jira 在入会时仅检查是否已提交，使用时才验证。凭据服务不可用则拒绝入会。以下早期版本中相反的入会规则以本条为准。
>
> **v1.5（2026-11-14）**：不在建会时预取 NSDP。gateway 的受控查询工具报告授权缺失或失效时，经 outbox 通知组织者；会议其余判断继续。`nsdp_hold` 为兼容保留字段，正常运行会清除旧 hold。

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
| `session_paused` | 会话 paused（owner 离会）暂停辅助中；仅 ingest 面使用（CONTRACT_RECORDER_STREAM v1.1），API 面以 `phase` 字段表达 | 告知原因（暂停中，owner 回会后恢复） |
| `not_owner` | 操作者不是该会话/订阅的 owner | 回答"只有会话 owner 可以…" |
| `meeting_ambiguous`（v2） | 同会议号在时间窗内命中多个候选场次，无法消歧 | 请用户稍后在目标场次时间窗内重试 |
| `session_claimed`（v2） | 该实例已有**他人**的非 ended 会话（先到先得，归属不可转移） | 告知"该会议已由他人启动会参谋" |
| `instance_ended`（v2） | 该实例已有会话且已 ended/draining（每实例一生一个 session，不可复建） | 告知"该场会议的辅助已结束，不可重新开始" |
| `claim_conflict`（v2） | 申请人在其他非 ended 会话已有归属（owner 或订阅，requested 也算占位）；或 owner 申请订阅自己的会话 | 告知"你已归属另一场未结束的会议，先结束/退出后再试" |
| `not_supervisor`（v2） | 既非该日历事件组织者，也不在 supervisor（小C）名单 | 告知"仅组织者或管理员授权的参会者可启动" |
| `not_invited`（v2） | 名单内小C 但不在该场日历邀请名单（也非组织者） | 告知原因 |
| `identity_unresolved`（v2） | supervisor 名单配置时身份无法唯一解析（§9.4） | 面向 admin 显示，不转发用户 |
| `invalid_state` | 状态机非法迁移（含对 `expired` 行审批、终态订阅行 decide 等） | 按 §6.6 对应话术回复 |
| `internal` | 服务端内部错误 | 提示稍后重试 |

**NSDP 门（v1.1）**：组织者面全部指令（入会/会议列表/状态/结束会议/审批/组织者撤销）在执行前要求该 open_id 已有 NSDP token；无/失效 → 409 `authorization_required`（NSDP 变体），bot 每次都回新提交链接（被动持续提醒）。订阅面（小B 的申请/取消）**不经过**该门。会话进行中 token 失效：监督器置 `nsdp_hold=1`（挂起该会话全部模型决策），经 outbox 向小A 周期性 DM 新提交链接（默认 5 分钟一条）；重新提交成功后自动恢复。NSDP 数据由 advisor 侧取（noam 子进程，token 仅存在于子进程环境），以 `kind:"nsdp"` 材料文档进入模型上下文。**v1.2a（2026-11-13，T7）**：NSDP 取数不可达（超时/后端故障）**不再拒绝入会**——降级为无 NSDP 材料建会，监督器首个维护周期起自动补取（成功即入材料，401 即挂起并提醒）；token 隐式验活由建会时点移至该首次取数。取数侧对同一 token 短 TTL 缓存（默认 10 分钟），避免连续建会重复跑全量 `nsdp info`。

## 2. `POST /sessions` —— 入会（S1）

凭据缺失返回 409 `authorization_required`，`error.missing_credentials` 按 `feishu`、`nsdp`、`jira` 顺序列出缺失项；飞书项含 `authorize_url`，其他项含 `submit_url`。兼容旧客户端，首项同时复制到 `error` 顶层。链接仅发组织者，不含 token；提交链接一次性且有效期 10 分钟。检查先于新建或复用会话；提交本身不验活，NSDP/Jira 失效在实际查询时报告。

```json
{"owner_open_id": "ou_A", "meeting_ref": {"no": "123456789", "password": ""},
 "mic_enabled": false}
```

成功（201）：

```json
{"session_id": "s-3f9c", "meeting_no": "123456789", "title": "周会",
  "phase": "running", "meeting_instance_key": "123456789|cal_x/evt_y|1762801200",
  "started_at": "2026-11-10T14:00:00+08:00",
  "recorder_command_args": {"session": "s-3f9c", "event": "cal_x/evt_y",
    "url": "ws://server:8400/ingest", "token": "st-xxx", "mic": false}}
```

- 语义（v2 准入链，顺序固定）：凭据门（同 v1.6）→ 解析目标 occurrence（owner 日历 ±12h 窗口内按 `meeting_no` 匹配；0 个 → `meeting_not_found`，>1 个 → `meeting_ambiguous`）→ 时间窗（±30min，`outside_window`）→ 准入（组织者身份命中走 A 路径；否则须在 supervisor 名单且命中该场邀请名单或本人组织者，见 §9.3；失败 → `not_organizer` / `not_supervisor` / `not_invited`）→ 实例与占位唯一（STATE_MACHINES §0：非 ended 同事件绑定会话 → 本人幂等 `existing:true` / 他人 `session_claimed`；精确 instance key 已有 ended/draining 会话 → `instance_ended`；本人已有其他非 ended 归属 → `claim_conflict`）→ 建会（phase=`running`，写 controller claim，材料 = 日历事件描述）→ 铸流令牌 → 监督器启动。命令文本由 bot 按模板渲染（附录见 CONTRACT_RECORDER_STREAM）；**响应中不得含任何用户凭据**。
- v2 提醒义务（bot 话术层，SM-12 落实）：建会成功回复须提示「建会后 900 秒内需开始采集，超时本场失效且不可重建」。
- 凭据缺失/失效（409）：

```json
{"error": {"code": "authorization_required", "message": "user token missing/expired"},
 "authorize_url": "https://<域名>/oauth/start?state=…"}
```

  小A/小C 完成授权后重发入会指令（state 已绑定意图，auth 侧可提示续接）。
- 拒绝码全集：`authorization_required` / `meeting_not_found` / `meeting_ambiguous` / `not_organizer` / `not_supervisor` / `not_invited` / `outside_window` / `session_claimed` / `instance_ended` / `claim_conflict`。
- 幂等与并发：同一 owner 对同一实例的非 ended 会话重试 → 返回既有会话（`existing:true`，不重建、令牌不换）；并发双人抢占同一实例 → 事务级唯一约束，后到者 `session_claimed`；同 key 重放 → 原响应。

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
  "phase": "running", "started_at": "…", "idle_timeout_s": 900}]}
```

只返回 running/paused/draining 会话（v2：全部非 ended，按 started_at 倒序；"最近一场未结束会话" = 首元素）。

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

成功（200）：`{"session_id": "s-3f9c", "phase": "draining"}`。会话（running 或 paused）进入 draining：在途输入/模型任务/待发回答在**绝对排空截止**（STATE_MACHINES §1.3）内落定后自动置 ended。已 ended 再调 → 幂等返回 `{"phase": "ended"}`。非 owner → `not_owner`（日历组织者不是 owner 时同样 `not_owner`）。

## 4. 订阅申请（S3.1–S3.2）

### 4.1 `POST /subscription-requests`

```json
{"subscriber_open_id": "ou_B", "meeting_no": "123456789"}
```

成功（201）：

```json
{"request_no": 7, "session_id": "s-3f9c", "state": "requested",
  "approval_deadline_at": 1762801800.0,
  "owner_open_id": "ou_A", "title": "周会"}
```

- 会议号解析到唯一实例的 `running`/`paused` 会话才受理（v2：**paused 也受理**，已定 2026-11-16）；未命中 / draining / ended → `not_found`（bot 回"未找到该会议或会议已结束"）。
- 申请人在任何**非 ended** 会话已有归属（owner/订阅，`requested` 也算占位）→ `claim_conflict`（STATE_MACHINES §3.1）。
- `request_no`（即 #rN）**全局唯一**、单调递增，advisor 分配；`approval_deadline_at`（Unix 秒）为持久化审批截止，到期未决自动 `expired`。
- 审批请求的 IM 投递由 bot 直接完成（不走 outbox，不承诺可靠投递——bot 离线期间申请可丢，小B 重发即可）。

## 5. 审批与撤销（S3.3–S3.5）

### 5.1 `POST /subscription-requests/{request_no}/decide`

```json
{"owner_open_id": "ou_A", "decision": "approve" | "deny"}
```

成功（200）：`{"request_no": 7, "state": "approved" | "denied"}`。

- 仅 `requested` 可 decide；重复 decide（含各终态行）幂等返回当前状态；**对 `expired` 行 decide → `invalid_state`**（逾期审批不得复活，重申请走新行）；非该会话 owner → `not_owner`。
- `approve` 生效口径：**从下一条决策起**该收件人进入 fan-out 集合（仅前向，不回溯）。

### 5.2 `POST /subscriptions/{request_no}/revoke`

调用者必须是**会话 owner 或该订阅的 subscriber 本人**（二选一字段：`owner_open_id` / `subscriber_open_id`）。v2 按调用者与当前状态落不同终态：

| 调用者 | 当前状态 | 结果 | 语义 |
|---|---|---|---|
| subscriber 本人 | `requested` | `subscriber_cancelled` | **撤回申请**（v2 新增）：释放占位，可再申请新行 |
| subscriber 本人 | `approved` | `subscriber_cancelled` | 退订：取消该收件人 pending IM，释放占位 |
| owner | `approved` | `owner_revoked` | 撤销：即时止血（取消 pending IM），释放占位；**可重新申请**（新行重审） |
| owner | `requested` | `invalid_state`（409） | owner 对未审批行应使用 decide |
| 其他组合 | 非 `requested`/`approved` | `invalid_state`（409） | 终态行幂等回显见下 |

- 终态行重复 revoke → `200 {request_no, state: <当前终态>}`（幂等回显，含旧 `revoked` 历史行）。
- 撤销/退订即刻止血：该收件人 pending 的 IM 行置 `cancelled`，后续决策不再 fan-out；已 `sent`/`uncertain` 不追回。

## 6. 与 REQUIREMENTS S3 边界的逐条对应

| S3 边界 | 契约落点 |
|---|---|
| 仅前向，不回溯 | §5.1 approve 只影响后续决策的 fan-out 集合 |
| 小B 只收回答，非字幕/录音 | outbox 行仅 `kind=reply` 的 IM 投递（§7） |
| 小B 消息不进模型上下文 | 订阅面无任何写会话上下文的资源 |
| 审批请求不承诺可靠投递 | §4.1 bot 直发，不走 outbox |
| 已批准分发走 outbox，可靠 | §7 pending/result 状态机 |
| approved 再申请幂等 | §6.1 |
| denied / expired / subscriber_cancelled / owner_revoked 再申请 | §6.2/§6.3 一律新行新 request_no、重新审批 |
| ended 拒新订阅 | §4.1 draining/ended → not_found |

### 6.1 approved 再申请（幂等提示，不新建）

```json
{"subscriber_open_id": "ou_B", "meeting_no": "123456789"}
→ 200 {"request_no": 7, "state": "approved", "existing": true}
```

### 6.2 denied / expired / subscriber_cancelled 再申请（新建审批）

```json
→ 201 {"request_no": 12, "state": "requested",
        "previous": {"request_no": 7, "state": "denied"}}
```

### 6.3 owner_revoked 再申请（v2：允许，推翻 v1 终态拒绝）

```json
→ 201 {"request_no": 13, "state": "requested",
        "previous": {"request_no": 7, "state": "owner_revoked"}}
```

v1 中「revoked 终态再申请 → 409」作废（已定 2026-11-16）；旧 `revoked` 历史行同样可再申请。bot 不再使用"该订阅已被组织者撤销（不可再申请）"话术。

### 6.4 审批超时（v2 新增）

`requested` 行到期未决 → 系统 `expired` 并释放占位；bot 对 subscriber 的「状态」查询显示过期状态；逾期的 decide 返回 `invalid_state`，subscriber 重新申请即可。

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

- 会话集（v2）：该 open_id 名下全部**非 ended** 会话，按 `started_at` 倒序——`running`（含完整 `recorder_command_args`，可启动采集）；`paused`（`recorder_command_args` 为 `null`，GUI 展示"owner 离会暂停中"，**不自动重开**，恢复 running 后用户手动重新开始采集）；`draining`（`null`，仅展示）。args 组装复用建会逻辑（§2），白名单与 CONTRACT_RECORDER_STREAM 附录 A 一致。
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

### 9.3 `GET /admin/supervisors` —— supervisor（小C）名单（v2 新增）

```json
{"supervisors": [{"login_name": "kai.han", "open_id": "ou_C",
  "enabled": true, "updated_at": 1762800000.0, "updated_by": "admin"}]}
```

- 返回全部名单行（含禁用与未解析项：`open_id` 为 `null` 即未解析/多解，不参与准入）。
- 名单是**会话 owner 权限面**：admin-site 维持 localhost-only/受信网络边界（REQUIREMENTS 非目标：公网匿名访问）；部署面扩大前必须先加认证。

### 9.4 `PUT /admin/supervisors` —— 配置/启停（v2 新增）

```json
{"login_name": "kai.han", "enabled": true}
```

→ 200 `{"login_name": "kai.han", "open_id": "ou_C", "enabled": true, "updated_at": …}`。

- `login_name` 形如 `kai.han`：advisor 以 `<login_name>@<配置的默认邮箱域>` 在身份目录（`users` 表 + auth profiles）中解析唯一 `open_id`；也可直接提交完整邮箱或 `ou_` 开头的 open_id。
- 解析失败/多解 → 409 `identity_unresolved`（行保留 `open_id=null`、`enabled=false`；该用户完成一次 GUI 登录/飞书授权进入目录后可重新启用）。**名字缺失/多解一律拒绝启用，不得以展示名作权限主键。**
- 启停/删除幂等；写审计（`updated_by` + 时间戳持久化）。名单移除对已启动 session 的影响：**不追溯**——已建会话不因名单变更终止/迁移（准入只在建会时校验）。
- bot/GUI 不可经任何端点声明 supervisor 身份（本面仅 §9 admin 入口，localhost 边界内）。

## 10. 版本

API v1（响应体保留 `"api": 1` 字段位，首版不强制）；v1.1 NSDP 门；v1.2 GUI 面（§8）；v1.3 运维面（§9）；v1.4 §9.1 身份目录与订阅列表（依赖 CONTRACT_AUTH v1.3）；**v2.0（2026-11-16）状态机 v2**：phase 更名/新增 `paused`、实例唯一建会、单人单占位、订阅新状态与重申/撤回、§9.3/§9.4 supervisor 名单面、GUI §8.3 paused 展示。不兼容变更递增版本号并同步 bot/advisor/macos-gui/admin-site 侧实现、测试与 `integrations/` 用例；v2.0 为同仓整体切换（单机 `local-up` 一次拉起全部节点），无 v1/v2 双轨窗口。
