# 架构 · 方案 C（v0.4 草案）

状态：设计基线。相对旧方案 B 的决策翻转与动机见 [new/README](../README.md)；需求场景见 [REQUIREMENTS](REQUIREMENTS.md)。
修订：v0.2 新增 bot 控制面（入会指令）、云端 OAuth 鉴权、流令牌准入、会话监督器；v0.3 将 bot 与 auth 拆为独立部署节点（不再驻 advisor 进程内）；v0.4 评审修订：ingest 改为每条音频源一条连接、授权链接统一经 advisor 下发（bot 不直连 auth）、删除暂停/恢复、订阅 revoked 定为终态、材料来源定为建会时从日历事件描述自动带出、拓扑图编号修正。

## 1. 节点拓扑

```
      小A 的 laptop                        云端服务器（单机 · 五个独立部署节点）             飞书云
┌─────────────────────────┐      ┌───────────────────────────────────────────────┐     ┌─────────────┐
│ 飞书客户端               │      │ bot（IM 控制面 · 通知面）                       │     │             │
│  · 开会（音频输出）       │◄─────┼─ ④recorder命令 · ⑪回答 · ⑫审批请求 IM ─────────┼────►│  会参谋bot   │
│  · 与bot聊天（①入会指令） │      │  · 指令解析 · 订阅审批交互                      │  ①④ │  (自建应用   │
│  · 浏览器完成OAuth ②     │      │  · outbox 待发IM 拉取/回写                     │  ⑪⑫ │   长连接)    │
│                         │      │ ──── ⑩ 内部API（localhost）─────────────────── │     │             │
│ local-recorder          │      │ advisor（会话核心）                             │     │ 小A、小B    │
│  · 双路采集 loopback+mic │      │  · ingest 网关（WS·流令牌鉴权·独立线程）        │     │ 都在这里聊天 │
│  · MuteGate 隐私门控     │      │  │    ⑤音频上行 → ⑥云内转PCM → ⑦final落库     │     └─────────────┘
│  · 断线缓冲             │─────►│  · 会话域（按用户隔离）                          │
│  · 整场音频本地留存      │  ⑤  │  │    sessions·字幕·材料·jobs·outbox·订阅       │
│    (不上传)             │      │  · 会话监督器：建会即consume · 排空收尾         │
│  · 零用户凭据，仅持流令牌 │      │  │    ⑧推理→gateway ⑨复核→fan-out             │
└─────────────────────────┘      │ ──── ⑩ 内部API：取有效用户凭据 ───────────────── │
                                 │ auth（OAuth 鉴权服务）                          │◄──── ② 授权回调
   小B：什么都不装，             │  · 授权链接·回调·换取·刷新·凭据保管              │     （小A 的浏览器）
   只在飞书里和bot说话           │  · 用户凭据按 open_id 隔离，云端保存             │
   （消息经飞书云路由至bot）      │ stt-server（内网WS）⑥⑦                          │
                                 │ llm-gateway（内网HTTP）⑧ └ OpenCode(GLM) 沙箱  │
   ④ 小A 粘贴执行recorder命令    └───────────────────────────────────────────────┘
      → ⑤ 音频持续上行

  ① 小A 对 bot："入会 <会议号|链接>"，bot 经内部API 转给 advisor
  ② 首次/失效：advisor 返回授权链接 → bot 转发小A → 浏览器OAuth → 回调auth → token云端保存
  ③ advisor 校验（用 auth 提供的 user token 读日历）：会议存在 + 小A 是组织者 + 时间窗；事件描述顺带存为会话材料
  ④ 通过：advisor 建会 + 铸流令牌 + 监督器自动consume → bot 下发 recorder 命令 → 小A 粘贴执行
  ⑤ 音频上行（流令牌鉴权，int16/16k/mono，每路约 32 KB/s；每条音频源一条 WS 连接）
  ⑥ ⑦ ⑧ ⑨ 均为云内调用；⑩ = 云内 localhost API：bot↔advisor、advisor↔auth
  ⑪ bot 从 advisor outbox 拉取待发行并发送（回答 IM，uuid 幂等）；⑫ 订阅审批请求 IM（bot 直接发，不走 outbox，不承诺可靠投递）
  订阅/审批/会话状态全部落在 advisor 会话域；bot 无业务持久状态
```

## 2. 模块职责与边界

| 模块 | 部署位置 | 拥有的状态 | 不承担的职责 |
|---|---|---|---|
| `recorder` | 小A 的 laptop | 流令牌（短期，仅本会话）、断线缓冲、整场原始音频 | 不做 OAuth/选会（已上云）、不做 STT、不做业务判断、不上传音频外任何内容 |
| `advisor` | 云端（独立节点） | 会话、字幕、材料、模型任务、决策 outbox、订阅/审批、组织者/时间窗校验结果 | 不持有用户 OAuth 凭据（经 auth API 取）；不碰飞书 IM；不直接执行模型（调 gateway）；不自建 STT |
| `bot` | 云端（独立节点） | IM 会话去重等运行时态（无业务持久状态） | 不落业务状态；业务操作全经 advisor 内部 API；指令解析不进 LLM |
| `auth` | 云端（独立节点） | 用户 OAuth 凭据（按 open_id 隔离）、授权会话（state/一次性码） | 不做业务校验（组织者/时间窗校验在 advisor）；不碰 IM |
| `stt-server` | 云端（内网） | 识别器实例（短生命周期） | 不鉴权（内网模式）、不落任何业务状态 |
| `llm-gateway` | 云端（内网） | 远程任务、执行实例、短期结果 | 不感知用户/会议语义（沿用旧契约不变） |

云内互调全部 localhost：bot→advisor（指令/订阅/待发 IM）、advisor→auth（取有效凭据）、advisor→stt-server、advisor→llm-gateway。节点拆机部署时补服务间认证（见 DEPLOYMENT §3）。

模块独立原则沿用旧仓库：各自配置、环境、测试；进程间只经书面契约（WS/HTTP）交接，不导入彼此源码。

## 3. 关键架构决策（显式记录）

### D1 业务状态上云（对方案 B 的翻转）

旧边界"本地是会议业务状态的事实来源"反转为"**云端 advisor 是事实来源，按用户隔离**"。理由：bot 闭环（订阅审批需要服务端可写状态）、常在线（不随 laptop 休眠停摆）、多用户。代价：会议内容集中存储——部署受信边界内接受；公网运营前需按用户隔离+保留/删除策略复核（见 DEPLOYMENT §5）。

### D2 音频经 advisor 中转（单入口）

laptop 对云端只有一种入口：ingest WS（流令牌鉴权）。**每条音频源一条连接**——loopback 常开一条，开启麦克风时 mic 独立再加一条，两路连接互不影响（沿用旧 STT 契约"每连接承载一个音频源会话"的已验证语义）；recorder 命令中的 `--url` 即指向 ingest，不直连 stt-server。advisor 校验流令牌后，每条连接在云内转 PCM 给 stt-server 的一个识别会话；stt-server 退化为纯内网、去掉旧 v2 飞书鉴权（组织者校验逻辑移入 advisor，见 D3）。
代价：advisor 承担实时中转——ingest 通路独立线程/进程，与业务循环只经持久化队列交接（音频背压不得阻塞模型循环）。

### D3 鉴权是 Day 1 需求（凭据两类，用户凭据全在云端、由 auth 保管）

- **用户 OAuth 凭据**（user_access_token/refresh_token）：由独立的 **auth 节点**经飞书授权页获取并**按 open_id 云端保存、云端刷新**（换取/刷新/一次性 refresh 轮转语义沿用旧 recorder oauth.py，redirect URI 指向 auth 的 HTTPS 回调）。advisor 经 auth 的内部 API 获取有效 token，用后不留存；**bot 不直连 auth**——凭据缺失/失效时 advisor 在建会响应中携带授权链接，由 bot 转发小A（小A 全程不接触 token 明文）。组织者/时间窗校验由 advisor 执行（用 auth 提供的 token 读日历，规则照抄旧 stt-server v2）。
- **会话流令牌**（短期、仅绑定单一 session）：advisor 在建会时铸造，经 bot 以 IM 命令文本下发，recorder 在 ingest WS 建连时出示，会话结束即失效。
- **红线：user_access_token 绝不进入 IM 命令文本**（聊天记录是泄露面）；laptop 不保存任何用户凭据。
- stt-server / llm-gateway 保持内网无认证。

### D4 bot 是独立部署节点，兼控制面与通知面

- **控制面**：小A 的全部操作经 bot 指令（`入会/结束会议/审批/撤销/状态`）。指令解析 v1 规则式（会议号/链接解析沿用旧 `parse_meeting_reference` 语义），**不进 LLM**；解析结果经 advisor 内部 API 触发会话操作。
- **通知面**：outbox 的 IM 行仍由 advisor 会话域持有并走原状态机；bot 作为 IM 传输执行者，经内部 API **拉取待发行、回写发送结果**（sent/uncertain）。原 `recipients` 静态配置的等价物 = 订阅表中 `(session, state='approved')` 的集合：组织者行在建会时自动写入，小B 行经审批写入。每条决策 fan-out 落 outbox 多行（`local` 行恒存为事实来源 + 每收件人一行 IM 行），发送状态机、幂等 UUID、失败 uncertain 人工裁决——全部沿用旧 advisor outbox 语义。
- bot 无业务持久状态，重启不影响会话域；bot 短暂下线仅意味着通知/审批延迟。

### D5 可靠性语义平移，不降级

旧 spool 契约的幂等语义（event_id/sequence 去重、缺口等待、stream_end 确认、断线续传）原样搬进新的 recorder↔advisor 网络契约；advisor→gateway 的幂等 request_id 契约不动；一次 HTTP 断开 ≠ 任务失败/取消；迟到结果按规则丢弃或复核——旧 plan.md §3 的边界条目逐条继承，本文不重复。

### D6 会话生命周期由指令驱动、监督器执行

服务器上无人工 CLI。bot 建会成功 → **会话监督器**自动启动该会话的 consume 循环；`结束会议` 指令或采集超时 → draining → 输入/模型任务/待发回答全部落定 → 自动置 ended（排空语义沿用旧 advisor 会话收尾）。监督器随 advisor 进程常驻，管理全部并发会话的循环启停。

## 4. 核心数据流与状态机

### 4.0 启动一场会议（S1 时序）

```
小A ──①"入会 123456789"──► bot ──⑩──► advisor：查该用户凭据（经 auth API）
                                ├─ 无/失效 ──► ②advisor返授权链接 → bot转发小A → 浏览器OAuth → 回调auth → token云端保存
                                └─ 有效 ──► ③读日历校验（存在+组织者+时间窗）
                                            ├─ 不通过 ──► bot 告知原因，止
                                            └─ 通过 ──► 建会 + 铸流令牌 + 监督器启动consume
                                                        ──④ recorder 命令 IM 小A ──► 粘贴执行 ──► ⑤音频上行
```

### 4.1 回答分发（S2/S3 主链路）

```
音频 → 字幕(final) → 会话上下文（字幕 + 建会时带出的会议描述材料）→ 模型任务(gateway) → 决策复核
  → outbox: 1 行 local（事实来源）+ N 行 im（组织者 + approved 小B，逐行独立状态机）
  → bot 逐行发送（uuid 幂等）→ sent / uncertain（人工裁决工具沿用）
```

订阅集合在**每次决策时**实时查询：审批通过即刻对下一条回答生效，撤销即刻止血（撤销时同时 cancel 该收件人的 pending IM 行）。

### 4.2 订阅状态机

```
requested ──小A同意──► approved ──小A撤销──► revoked（终态）
    │
    └──小A拒绝──► denied ──小B再申请──► requested(新)
```

仅 approved 收分发；仅前向，不回溯。`revoked` 为终态：小B 再申请不新建、直接告知已撤销。

### 4.3 会话生命周期

```
recording ──结束指令/采集空闲超时──► draining ──输入/任务/回答全部落定──► ended
```

- 采集空闲超时阈值（无上行/心跳的持续时间）为 advisor 配置项（config），超时即转 draining。
- draining 与 ended 均拒绝新订阅；ended 拒绝迟到模型结果（沿用旧语义）。
- 无 paused 状态：v1 不提供暂停/恢复辅助（见 REQUIREMENTS 非目标）。

## 5. 契约清单（随实现冻结）

| 契约 | 两端 | 替代/来源 |
|---|---|---|
| recorder ↔ advisor 流式接入（WS，流令牌鉴权） | 采集器 ↔ advisor ingest 网关 | 新写；语义源自旧 CONTRACT_RECORDER_INPUT |
| OAuth 授权与回调（HTTPS） | 小A 浏览器 ↔ auth | 语义源自旧 recorder oauth.py；redirect URI 指向 auth，须在飞书应用后台精确注册 |
| bot ↔ advisor 内部 API（localhost） | bot ↔ advisor | 指令触发会话操作、订阅读写、outbox 待发 IM 拉取/回写、凭据缺失时返回授权链接 |
| auth 内部 API（localhost） | advisor ↔ auth | 获取有效用户凭据（含静默刷新）与授权 URL 生成；bot 不直连 auth |
| STT 流式（WS） | advisor ↔ stt-server | 沿用旧 CONTRACT_STT_STREAM，auth 降为 none |
| 远程推理（HTTP） | advisor ↔ llm-gateway | 沿用旧 CONTRACT_REMOTE_INFERENCE，不变 |
| 飞书 IM 事件/发送 | bot ↔ 飞书开放平台 | 权限清单沿用旧 docs/APP_PERMISSIONS_EVENTS.md 已验证条目 |
| recorder 启动命令（IM 文本） | bot → 小A → 终端 | 参数：session、event、url、token、mic 开关 |

## 6. 实施阶段（建议）

- **C0 冻结契约**：流式接入契约（含流令牌）、OAuth 回调流程、订阅/审批与指令 API 面；建模块骨架。
- **C1 单用户上云**：bot 收"入会"指令 → OAuth（真链接或桩）→ 建会 → 命令下发 → recorder 推流 → 字幕 → gateway（假模型）→ 决策 → IM 回小A；S1–S3 端到端（含假飞书桩）。
- **C2 多租户**：按用户隔离、配额、订阅并发；真实 GLM 与真实飞书联调。
- **C3 收尾**：部署/监控/保留策略、旧仓库资产归档核对、真实会议验收取证。
