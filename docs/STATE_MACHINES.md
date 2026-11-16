# 状态机规格 · 会话与订阅（v2 冻结）

> **v2（2026-11-16，SM-01）**：落实 [REQUIREMENTS v0.4](REQUIREMENTS.md) 与 [SERVICE_STATE_MACHINES_V2](SERVICE_STATE_MACHINES_V2.md)/[DOMAIN_MODEL_V2](DOMAIN_MODEL_V2.md) 的业务口径（决策记录见 [TODO_STATEM_MACHINES](../TODO_STATEM_MACHINES.md) SM-00）。v1 的 `recording` 更名 `running` 并新增 `paused`；订阅新增 `expired`/`subscriber_cancelled`/`owner_revoked` 且撤销后可重申；会话按会议实例唯一；draining 有绝对截止时间。本文是 advisor 实现与测试的对照规格；API 错误码见 [CONTRACT_ADVISOR_API](CONTRACT_ADVISOR_API.md) §1.1；ingest 侧错误码见 [CONTRACT_RECORDER_STREAM](CONTRACT_RECORDER_STREAM.md) §3.4。变更需同步本文与两侧契约。

## 0. 术语与全局不变量

- **会议实例（meeting instance）**：一场具体会议 = 会议号 + 权威日历 occurrence。`meeting_instance_key = "{meeting_no}|{event_calendar_id}|{event_id}|{occurrence_start_unix}"`，四段全部取自**同一权威 occurrence**（经 owner 用户 OAuth 读到的日历事件实例）；`occurrence_start_unix` 为该场次固定排期开始时刻。同号跨天、同日两场、跨时区均产生不同 key。
- **每实例至多 1 个 session（含 ended）**；draining/ended 实例不可复建（含 900 秒无音频报废，业务已接受）。
- **session owner**：合资格小A（该 occurrence 的日历组织者）或合资格小C（admin 名单 enabled + 该场日历邀请名单命中或本人组织者），先到先得、创建后固定不变。日历组织者不是 owner 时，对该 session 无任何权限（不能接管、不能订阅、其离会不影响 phase）。
- **单人单占位（SessionClaim）**：任一用户在**非 ended** 会话上至多 1 条 active claim（kind=controller 或 subscriber）；订阅 `requested` 即占位。denied / subscriber_cancelled / expired / owner_revoked / ended 释放；paused / draining 不释放。
- **所有时限为配置项**（§5），除无音频 **900 秒**为业务定值外，其余默认值为工程初始值，不构成业务定值。
- `uncertain` 不伪装已送达；`sent` 不可撤回；迟到结果不复活任何终态。

## 1. 会话状态机

状态集：`running` / `paused` / `draining` / `ended`。`ended` 终态。**建会成功即 `running`**（不等待飞书会议开始、GUI 采集或 STT/模型健康）。

| # | 从 | 迁移 | 触发 | 守卫 / 副作用 |
|---|---|---|---|---|
| S1 | — | → `running` | `POST /sessions` 成功 | 准入链（CONTRACT_ADVISOR_API §2）；写 controller claim；铸流令牌；监督器启动 |
| S2 | `running` | → `paused` | 确认 owner **在会→离会**（§4 观测输入） | 仅 session owner；创建时未入会/在会未知/查询失败/音频中断/GUI 停录均不算离会。副作用见 §1.2 |
| S3 | `paused` | → `running` | 确认**同一** owner 回会 | 沿用同一 session、订阅与上下文；不自动重开采集（已定：手动）；恢复后新字幕正常驱动新决策 |
| S4 | `running`/`paused` | → `draining` | owner `end` / admin end / 确认会议结束 / 900 秒无真实音频 | 写绝对 `drain_deadline_at`；拒新源、新订阅、新模型任务；900 秒在 paused 下**连续计时不冻结**；误结束不可逆 |
| S5 | `draining` | → `ended` | 排空条件全部满足（§2） | 监督器自动；释放全部 claim；吊销流令牌 |
| S6 | `draining` | → `ended` | `drain_deadline_at` 到达仍有未完成项 | 强制收尾（§1.3）；写诊断记录；释放全部 claim |
| S7 | `draining`/`ended` | （不变） | 迟到音频/字幕/模型结果/观测快照 | 快照时效校验；不复活、不应用 |

- 幂等：重复 `end` 回显当前 phase；`paused`/`draining`/`ended` 无任何用户操作可逆向。
- `running → ended` 不允许（必须经 draining；旧 v1 的瞬时合并路径废除，S5 可即时达成等价效果）。

### 1.1 900 秒无音频规则（唯一业务定值）

- 空闲截止 = `max(created_at, last_audio_at) + idle_timeout_s(900)`，**绝对时刻**、持久化计算，重启不漂移。
- **真实音频** = ingest 二进制帧；心跳/ping、`gap`/`stop`/`stream_end`、控制 JSON、建连握手一律**不续期**。
- running 与 paused 连续计时（含 GUI 手工停录的空窗、owner 离会的空窗）；draining/ended 不再计。
- 建会即起算：提前超过 900 秒建会且未开录 → S4 → 实例报废（已定：接受；bot/GUI 负责建会话术提醒）。
- 停录后 900 秒内对同一 `running` session 再次 start（`resume:false` 新纪元）即续用，不新建会话。

### 1.2 paused 的即时副作用（已定「立即作废」）

- 在途模型 job → `abandoned`；未开始发送的 pending IM 行 → `cancelled`；不再发起新 job。
- ingest 拒新连接、拒收在途（错误码 `session_paused`，冲刷后关闭）；已有会话事实与审批历史保留。
- 已 `sent`/`uncertain` 的投递不撤回、不伪装。
- S3 恢复后：被取消的内容不重发，新字幕从新决策开始。

### 1.3 draining 总预算（绝对截止）

- 进入 draining 时写 `drain_deadline_at = now + drain_deadline_s`（配置，默认 600s）；**不可因重启/重试/再轮询重置**；advisor 重启只恢复剩余预算。
- 正常排空完成可提前 ended（S5）。
- 截止时（S6）按序执行：未终态源 → `lost`/`abandoned`；pending|running job → `abandoned` 并忽略迟到 gateway 结果；未开始发送的 pending IM → `cancelled`；`sending` → `uncertain`；写诊断行（各项计数与原因）→ `ended` → 释放全部 claim。

## 2. 排空完成条件（S5）

1. 每个已声明源 `drained`/`lost`/`abandoned`（从未连接的源在进入 draining 后即 `abandoned`）。
2. 无 in-flight 模型 job（终态或超过任务 deadline）。
3. outbox 无 `pending` 行（`sending` 等回写或预算转 `uncertain`；`uncertain` 不阻塞 ended，留人工裁决）。
4. 回答有效期（§5 `reply_validity_s`）在 running/paused 期间同样生效：过期且未开始发送 → `cancelled`，不因 bot 长期离线无限堆积。

## 3. 订阅状态机

状态集：`requested` / `approved` / `denied` / `expired` / `subscriber_cancelled` / `owner_revoked`（历史行可含旧值 `revoked`，只读兼容）。**全部终态不逆向**；重新申请一律**新行新 request_no**，不复活旧行。

| # | 从 | 迁移 | 触发 | 约束 / API 结果 |
|---|---|---|---|---|
| T1 | — | → `requested` | `POST /subscription-requests` | 目标会话 `running`/`paused`（**已定：paused 受理**）；写 subscriber claim（占位）；行内持久化 `approval_deadline_at`；`201 {request_no, state, approval_deadline_at}` |
| T2 | `requested` | → `approved` | owner `decide=approve` | 决策者 = session owner；即刻进入 fan-out（仅前向） |
| T3 | `requested` | → `denied` | owner `decide=deny` | 同上；释放占位 |
| T4 | `requested` | → `expired` | 审批 deadline 到期（系统） | 释放占位；**迟到 decide → 409 invalid_state**；重申请新行 |
| T5 | `requested` | → `subscriber_cancelled` | 申请人本人撤回（**v2 新增，已定**） | 释放占位；再申请新行重审 |
| T6 | `approved` | → `subscriber_cancelled` | 本人退订 | 取消该收件人 pending IM；释放占位；同会话再申请新行重审 |
| T7 | `approved` | → `owner_revoked` | owner 撤销 | 即时止血（取消 pending IM）；释放占位 |
| T8 | `denied`/`expired`/`subscriber_cancelled`/`owner_revoked` | →（新行）`requested` | 同人再申请 | **owner_revoked 后允许重申（已定 2026-11-16，推翻 v1 §6.3 禁止口径）**；新 request_no、重新审批、不回溯 |

### 3.1 幂等与拒绝

| 情形 | 响应 |
|---|---|
| `approved` 再申请 | `200 {state: approved, existing: true}`，不新建 |
| 对已决 `approved`/`denied`/本人取消/owner 撤销行重复 `decide` | `200 {当前状态}`（幂等回显，不复活旧行） |
| 对 `expired` 行 decide（逾期审批） | `409 invalid_state`（不得复活） |
| 本人撤回/退订作用于 `requested`/`approved` 之外 | `409 invalid_state` |
| owner 撤销作用于 `requested` | `409 invalid_state`（owner 对未审批行用 decide 表态） |
| 目标会话 `draining`/`ended` 时申请 | `404 not_found` |
| 申请人在其他**非 ended** 会话已有 active claim（owner 或订阅） | `409 claim_conflict` |
| owner 申请自己会话 / 同会话重复申请占位 | `409 claim_conflict`（controller claim 已存在） |
| 非 owner 的 decide/撤销 | `409 not_owner` |

### 3.2 不变量

- 仅 `approved` 收件人 + owner 进 fan-out；集合在每次决策时实时查询；仅前向。
- 每次状态迁移与 SessionClaim 释放/建立**同事务**。
- 会话 ended 后订阅行不迁移（历史保留），claim 全部释放。
- 审批通知 IM 由 bot 直发（不承诺可靠投递，沿用 v1）。

## 4. 会话观测输入（paused/结束联动）

- 输入为**快照**：`{owner_presence: present|left|unknown, meeting_status: in_progress|ended|unknown, observed_at}`，来源两档：SM-06 前**桩注入**（内部接口，供 S2/S3/S4 验收）；SM-08 起真实轮询（用户身份链路，见 studies/meeting-events 与 CONTRACT_AUTH v1.6）。
- 触发规则：S2 需「已确认 present → 已确认 left」**转变**（创建时 left/unknown 不算）；S3 需 paused 下确认同一 owner present；S4（会议结束）需确认 ended。
- 一切查询失败/超时/查无结果/重复会议号未消歧/**快照年龄超过 `max_observation_age_s`** → `unknown`，不迁移、不伪造。
- 非 owner 用户的进出不影响任何迁移。

## 5. 时限与到期动作总表（配置项）

| 项 | 配置名 | 默认（工程初始值） | 到期动作 |
|---|---|---|---|
| 无音频（业务定值） | `idle_timeout_s` | 900 | S4 → draining |
| 排空总预算 | `drain_deadline_s` | 600 | S6 强制 ended |
| 审批等待 | `approval_ttl_s` | 1800 | T4 expired + 释放占位 |
| 回答有效期 | `reply_validity_s` | 3600 | 未发送 cancelled；发送中依回写/预算转 uncertain |
| 观测快照时效 | `max_observation_age_s` | 60 | 视为 unknown |
| 观测轮询间隔 | `observe_poll_s` | 15 | —（退避上限 `observe_backoff_max_s` 默认 120） |
| 观测单次调用超时 | `observe_timeout_s` | 10 | 记 unknown |
| 单次模型调用 | `model_timeout_seconds` | 90（现有） | 失败/重试受总预算约束 |
| 单次 IM 发送回写 | bot 侧现有 | — | `uncertain` 语义沿用 |

与 SERVICE_STATE_MACHINES_V2 §5.2 逐条对应；具体秒数除 900 外均未业务定值。

## 6. 数据模型与迁移（schema v4 → v5 → v6 → v7）

### 6.1 变更

- `sessions`：`phase` 枚举 `recording→running` 更名并扩充 `paused`；新增 `meeting_instance_key TEXT NOT NULL`（UNIQUE 索引）、`organizer_open_id TEXT NOT NULL DEFAULT ''`、`last_audio_at REAL`、`drain_deadline_at REAL`。
- 新表 `session_claims(user_open_id, session_id, kind controller|subscriber, active INTEGER, created_ns, released_ns)`；**partial unique index** `ON session_claims(user_open_id) WHERE active=1` 实现单人单占位的事务级约束。
- `subscriptions`：state 扩充（§3）；新增 `approval_deadline_at REAL`、`closed_by TEXT`（subscriber|owner|system）。
- 新表 `supervisor_grants(login_name TEXT, open_id TEXT, enabled INTEGER, updated_ns INTEGER, updated_by TEXT)`，`open_id` 为空表示未解析/禁用。
- v6：`sessions.vc_meeting_id TEXT`（唯一非 NULL；仅确认与会 owner 当前所在会议及该 occurrence 绑定后赋值）。同版补齐早期 v5 数据库可能缺失的 `owner_presence` / `observed_at`。

### 6.2 迁移矩阵（幂等，`upgrade_schema` v4→v5）

1. 建 `session_claims`/`supervisor_grants` 两表与 partial unique 索引；`subscriptions` 加列。
2. `UPDATE sessions SET phase='running' WHERE phase='recording'`；`organizer_open_id` 回填 `owner_open_id`；`last_audio_at` 回填 `started_at`；`drain_deadline_at` 置 NULL（既有 draining 会话由监督器升级后首轮按 `now+drain_deadline_s` 补写并记日志）。
3. `meeting_instance_key` 回填 `'legacy|' || meeting_no || '|' || event_calendar_id || '|' || event_id`，冲突时追加 `'#'||rowid` 保证唯一。
4. 为历史会话补写 controller claim 行（`active = phase NOT IN ('ended')`）与历史 approved 订阅的 claim 行（同前），ended 会话不补。
5. `PRAGMA user_version=5`。**回滚**：v5→v4 不支持（枚举扩列不可逆），以升级前备份恢复。

6. v5→v6：检查 `sessions` 列表并补缺失的 `owner_presence` / `observed_at` / `vc_meeting_id`，建立 VC ID 唯一索引，写 `user_version=6`；v4→v6 先完成上述 v4→v5 再继续此步。回滚仍使用升级前备份。

7. v6→v7：建立 `subscriptions(state,approval_deadline_at)` 索引；旧 requested 行回填 `created_ns/1e9 + approval_ttl_s`（升级时工程默认 1800 秒），同用户历史多条未决只保留最新可占位一条，其余冲突项 `expired`；按非 ended 会话补 subscriber claim，确保启动后唯一约束生效。旧数据库按 v4→v5→v6→v7 顺序升级，回滚依赖升级前备份。

## 7. 与 v1 的差异与兼容窗口

- `recording→running` 更名 + `paused` 新增：advisor/bot/admin-site/macos-gui 同仓同步切换，无长期双轨；部署顺序 = 单机 `local-up` 整栈一次拉起（CONTRACT_ADVISOR_API §10 v2.0）。
- 订阅 `revoked` 拆分为 `subscriber_cancelled`/`owner_revoked` + 新增 `expired`；旧 `revoked` 行只读兼容，新写不再产生。
- 新错误码：`meeting_ambiguous` / `session_claimed` / `instance_ended` / `claim_conflict` / `not_supervisor` / `not_invited` / `identity_unresolved` / `session_paused`（ingest）。
- recorder 协议 `protocol=1` 不变（新增错误码属兼容扩展，CONTRACT_RECORDER_STREAM v1.1）。
- 旧 `(owner, meeting_no)` 去重废除，改实例唯一 + 单人占位（§0）。
