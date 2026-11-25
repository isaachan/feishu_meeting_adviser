# 事故诊断 · 会话卡死 running（s-4d547f8809224d86b850590ac50ed9ac）

- **日期**：2026-11-25
- **环境**：prod（本机 advisor 进程，PID 2509974，deployed-commit `276fd55`）
- **症状**：meeting_no 952930521（ONVO DMT Meeting，2026-11-25 15:09:33 建会）对应的飞书会议早已结束，但 advisor session 一直保持 `running`，持续接收 stt-server 字幕，并每 ~2 分钟发起一轮 LLM 决策（至止血时已至 generation 476，最后一批字幕均为 `<sil>` 静音）。
- **止血**：16:31:45 `POST /admin/sessions/{id}/end`（CONTRACT_ADVISOR_API §9.2）→ draining → STT/ingest 连接全部关闭 → 16:31:46 `ended`；recorder 重连被 `session_ended` 拒绝。**未修改任何代码。**

## 1. 时间线

| 时刻 | 事件 |
|---|---|
| 15:09:33 | 会话创建（`created_ns`，phase=running，idle_timeout_s=900） |
| 15:09 → 16:25 | 正常运行：loopback 音频→STT 字幕→LLM 决策，期间产出多条已 sent 回答 |
| ~16:26 起 | STT final 全部变为 `<sil>`（静音），loopback 帧仍持续上行 |
| 16:31:45 | admin end（人工止血）→ 16:31:46 ended |

数据佐证（advisor.sqlite3，只读查询）：

- `sessions` 行：`phase=running`、`vc_meeting_id=NULL`、`owner_presence=NULL`、`observed_at=NULL`（自建会起从未有任何观测写入）、`last_audio_at` 持续刷新（与 loopback `last_seen_ns` 同步推进）。
- `sources`：`loopback` 持续 `live`（止血时 acked_sequence=44350+，约 4380 秒音频）；`mic` 15:29 后即无更新。
- advisor.log：无任何 `meeting observation unknown/failed/authorization unavailable` 行——观测每 15 秒都在跑，但全部走了**无日志的静默分支**。

## 2. 根因（三层叠加，两道终局兜底同时失效）

### 2.1 飞书 `list_by_no` 对本应用永久拒绝

observer 解析会议首选 `GET /vc/v1/meetings/list_by_no`。prod 实测（owner user token 只读复现，2026-11-25 16:30）：

```
GET /vc/v1/meetings/list_by_no?meeting_no=952930521&start_time=...&end_time=...
→ {"code": 99991679}
```

`99991679` 属 `LIST_BY_NO_DENIED`（advisor/src/advisor/meeting_observer.py:20），observer 置进程级 `_lookup_denied=True` 后**永久降级**为 fallback：`GET /vc/v1/bots/user_active_meeting`。

### 2.2 fallback 语义只能看见「owner 此刻在会」的会议，且失败路径静默

`user_active_meeting` 只返回 owner **当前正坐在里面**的会议。本会话的降级链路从未产出过「恰好 1 个候选」：

- 建会时（15:09）owner 尚未入会（或该接口当时未按 meeting_no 匹配到），`vc_meeting_id` 绑定从未发生（DB 佐证：历史会话大多绑定成功，本会话自始为 NULL）；
- 会议结束、owner 离会后，该接口返回 `{"meetings": []}`（prod 实测复现）。

`_observe()` 对候选数 != 1 的处理是无声 `return`（meeting_observer.py:133-134）——**不写观测行、不打日志**。后果：

- `apply_observation()` 从未被调用 → `meeting_status` 永远不会变成 `ended`；
- 状态机 S4（确认会议结束 → draining，STATE_MACHINES §1 表）**永远无法触发**；
- 运维侧完全不可见：`observed_at=NULL` + 无日志 = 无从发现观测链路早已瘫痪。

### 2.3 900 秒无音频兜底被「静音帧」击穿

STATE_MACHINES §1.1 定义「真实音频 = ingest 二进制帧」——**未区分内容**。会议结束后 recorder 的 loopback 源不知道会议状态，仍持续上行音频帧（16:26 后全部为静音，STT final 均为 `<sil>`）；每个二进制帧的 ack 都续期 `last_audio_at`（supervisor._idle_expired 以 `max(started_at, last_audio_at) + idle_timeout_s` 为绝对截止），故 idle 截止时刻随每帧后移，**900 秒规则永不满足**。

### 2.4 合成效果

S4 的两个触发条件（确认会议结束 / 900 秒无真实音频）分别被 2.2 与 2.3 击穿，会话进入「永生 running」：持续收静音字幕、持续起 model job（约每 2 分钟一次）、持续消耗 gateway LLM 配额。无任何告警路径。

## 3. 证据清单

1. DB（只读）：`sessions`/`sources`/`inputs`/`outbox` 查询（见 §1）。
2. advisor.log：1630 条 `ingest progress`（loopback 帧持续）、`stt final ... text="<sil>"`、`model job start` 逐代推进、无观测相关日志。
3. prod API 复现（owner user token，GET 只读）：
   - `list_by_no` → `code=99991679`（含按会议实际时段的窗口，排除窗口选择问题）；
   - `bots/user_active_meeting` → `code=0, meetings=[]`；
   - `authen/v1/user_info` → `code=0`（token 本身有效，含 `vc:meeting.meetingevent:read` scope，排除凭证问题）。
4. auth 侧凭证文件：owner `ou_b9f89...`（Peng WANG）state=ready，scopes 含 `vc:meeting.meetingevent:read`，未过期。

## 4. 影响评估

- **成本**：16:26（静音开始）至 16:31（止血）间的 model job 约 3 轮为纯浪费；若未发现，将按 ~30 轮/小时持续烧配额直至 recorder 停止上行（recorder 不会自行停止——loopback 采集与会议状态解耦）。
- **正确性**：该时段 `control=none`、`reply=""`，未向用户发出错误内容；`<sil>` 字幕已入 transcript（噪音）。
- **同类风险**：历史上另有 9 个会话 `vc_meeting_id=NULL`（均靠 idle/admin end 收尾）——2.2 的「从未绑定」并非孤例；「会议结束 + loopback 静音续传」的组合则会让 idle 兜底同样失效，本事故是首次被观测到的完整叠加。

## 5. 为什么历史会话没出事（对照分析，2026-11-25 补充）

对 51 场历史会话（含本次）做了全量 DB + 日志交叉统计：

**正常路径（41/51，80%）**：owner 在会中期间，observer 的 fallback（`user_active_meeting`）每 15 秒轮询，一旦查到 owner 在会即绑定 `vc_meeting_id`，之后每轮直查会议详情，会议结束 status=3 → S4 → 自动 ended。首绑延迟 1.0–91.1 分钟不等（owner 常先建会后入会，绑定要等入会后某次轮询命中）。

**未绑定路径（10/51，含本次）**：观测从未绑定。其余 9 场全部正常收场，靠的是 idle 或人工：

| 场景 | 场数 | 收场方式 |
|---|---|---|
| 帧流会后停止（用户停录/关机） | 3（16e358e5、b4f9bae8、801d51bb） | idle 900 秒自动 ended |
| 用户主动停录后等不及 15 分钟 | 1（009ea2c5：10:41 双源同秒 closed，10:49 admin end） | admin end（非救火，再等 7 分钟 idle 也会响） |
| 测试会话即停 | 1（02c112d8：建会 2 分钟即 admin end） | admin end（非救火） |
| 「同号第二场」确定性绑定失败 | 4（bind_vc_meeting 同号互斥/唯一索引挡掉） | 同样靠 idle/人工收场 |
| **本次：帧流会后不停 + 未绑定** | 1（s-4d547f88） | **卡死，admin end 救火** |

**本次的触发条件 = 两个历史首次同时出现的因素：**

1. **观测未绑定**（§2.1+2.2，历史 18% 会话同病，但单独出现时无害）；
2. **会后 loopback 帧流不停**（历史未绑定会话从未遇到）：mic 源 15:29 停、loopback 一直跑到止血时，16:26 后全为静音帧但仍续期 idle 计时器。历史用户会后要么双源同停（009ea2c5 日志：mic+loopback 同一秒 closed）、要么关机；「只关 mic、loopback 挂着、电脑不关」是新的使用模式。

以前「对不上号」的会，录音都会停（idle 兜底可用）；「录音不停」的会，都对上了号（观测兜底可用）。**两条件交集首次出现** → 卡死。

**「为什么这场会从未绑定」的推测**（静默无日志，无法事后确证，标注为假设）：

- `list_by_no` 对本应用全局 denied（非本会话特有）；fallback `user_active_meeting` 查询的是 **owner 本人**在会状态——本场 77 分钟里从未返回这场会；
- 本场 owner（ou_b9f89）是**受邀者非组织者**（organizer=ou_dd86）。统计：owner 为受邀者的会话绑定失败率 7/24（29%），组织者本人会话 3/26（12%）；
- 「mic 早停、loopback 持续」的源形态恰好是**投屏/共用电脑**的典型配置（关 mic 防回声、loopback 收远端声音）——若 laptop 上的飞书客户端登录的不是 Peng WANG 本人（或他以旁听/电话方式参会），飞书不会把他登记为这场会的参会人，`user_active_meeting` 自然查空。此假设与上述统计及源行为互相印证，但无直接证据。

## 6. 修复方向（未实施，待决策）

| 方案 | 内容 | 代价 / 契约影响 |
|---|---|---|
| **B（建议）** | 静音不算真实音频：STT final `<sil>`（或 ingest 能量检测）不续期 `last_audio_at` | 需改 STATE_MACHINES §1.1 冻结口径 + ingest/STT 链路 + 测试；900 秒兜底即可终结此类会话 |
| A | 观测兜底迁移：连续 N 次观测 candidates=0/unknown 后进 draining | 违反「unknown 不迁移」（§4 冻结口径），需改契约；语义风险高（误伤尚在进行的会） |
| C | 最小止血：observer 静默分支补日志与诊断字段（observed_at/错误计数） | 行为不变，只解决「不可见」；不解决卡死本身 |

无论选哪条，**2.2 的静默失败必须先可观测化**（方案 C 是 B/A 的前置）。

## 7. 止血操作记录（已完成）

```
POST /admin/sessions/s-4d547f8809224d86b850590ac50ed9ac/end   (16:31:45, 200)
→ phase=draining → stt closing → ingest closed (loopback, frames=46721)
→ 16:31:46 drained to ended；recorder 重连被拒（ingest start rejected code=session_ended）
```
