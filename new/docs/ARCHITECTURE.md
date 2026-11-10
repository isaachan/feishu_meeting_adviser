# 架构 · 方案 C（v0.1 草案）

状态：设计基线。相对旧方案 B 的决策翻转与动机见 [new/README](../README.md)；需求场景见 [REQUIREMENTS](REQUIREMENTS.md)。

## 1. 节点拓扑

```
     小A 的 laptop                        云端服务器（单部署 · 多用户）                 飞书云
┌─────────────────────────┐      ┌──────────────────────────────────────────┐      ┌─────────────┐
│ 飞书客户端               │      │ advisor（多租户会话服务）                  │      │             │
│  · 开会（音频输出）       │      │                                          │      │             │
│  · 与会参谋bot 聊天       │◄─────┼─── ⑦ LLM reply / 审批请求 IM ────────────┼──────┤  会参谋bot   │
│                         │  ⑦⑧⑨ │ ┌──────────────────────────────────────┐ │      │  (自建应用   │
│ local-recorder           │      │ │ ingest 网关（WS，独立线程/进程）       │ │      │   长连接)    │
│  · OAuth登录+选会议      │──────┼─►│   ②音频上行 → ③转PCM给STT             │ │      │             │
│    (user_access_token)  │  ①②  │ │   ④final字幕 → 会话上下文(幂等落库)    │ │      │ 小A、小B    │
│  · 双路采集 loopback+mic │      │ ├──────────────────────────────────────┤ │      │ 都在这里聊天 │
│  · MuteGate 隐私门控     │      │ │ 会话域（按用户隔离）                    │ │      └─────────────┘
│  · 断线缓冲             │      │ │  sessions·字幕·材料·jobs·outbox·订阅    │ │
│  · 整场音频本地留存      │      │ ├──────────────────────────────────────┤ │
│    (不上传)             │      │ │ 消费循环（多会话 watch）                │ │
└─────────────────────────┘      │ │  ⑤推理任务 → llm-gateway(localhost)   │ │
                                 │ │  ⑥结果复核 → outbox fan-out            │ │
     小B：什么都不装，            │ ├──────────────────────────────────────┤ │
     只在飞书里和bot说话 ─────────┼─►│ advisor bot（IM 收发/订阅审批，同库）   │ │
                                 │ │  ⑧小B申请 → ⑨小A审批 → 订阅表落库     │ │
                                 │ └──────────────────────────────────────┘ │
                                 │ stt-server（内网WS，sherpa 流式） ③ ④     │
                                 │ llm-gateway（内网HTTP） ⑤                 │
                                 │   └─ OpenCode (GLM) 执行沙箱              │
                                 └──────────────────────────────────────────┘

  ① OAuth 授权与组织者/时间窗校验（recorder 发起，advisor 执行）
  ② 音频上行 WS（int16/16k/mono，双路约 64 KB/s）
  ③ ④ ⑤ ⑥ 均为云内 localhost/内网调用
  ⑦ ⑧ ⑨ 飞书 IM；bot 与 advisor 同机同库，订阅/审批闭环天然成立
```

## 2. 模块职责与边界

| 模块 | 部署位置 | 拥有的状态 | 不承担的职责 |
|---|---|---|---|
| `recorder` | 小A 的 laptop | OAuth 凭据、断线缓冲、整场原始音频 | 不做 STT、不做业务判断、音频外的任何上行 |
| `advisor` | 云端 | 会话、字幕、材料、模型任务、决策 outbox、订阅/审批、bot 会话 | 不直接执行模型（调 gateway）、不自建 STT（调 stt-server） |
| `stt-server` | 云端（内网） | 识别器实例（短生命周期） | 不鉴权（内网模式）、不落任何业务状态 |
| `llm-gateway` | 云端（内网） | 远程任务、执行实例、短期结果 | 不感知用户/会议语义（沿用旧契约不变） |

模块独立原则沿用旧仓库：各自配置、环境、测试；进程间只经书面契约（WS/HTTP）交接，不导入彼此源码。

## 3. 关键架构决策（显式记录）

### D1 业务状态上云（对方案 B 的翻转）

旧边界"本地是会议业务状态的事实来源"反转为"**云端 advisor 是事实来源，按用户隔离**"。理由：bot 闭环（订阅审批需要服务端可写状态）、常在线（不随 laptop 休眠停摆）、多用户。代价：会议内容集中存储——部署受信边界内接受；公网运营前需按用户隔离+保留/删除策略复核（见 DEPLOYMENT §5）。

### D2 音频经 advisor 中转（单入口）

laptop 对云端只有**一条 WS 连接**：音频上行与字幕/控制事件同路。advisor 校验准入后，云内转 PCM 给 stt-server；stt-server 退化为纯内网、去掉旧 v2 飞书鉴权（组织者校验逻辑搬到 advisor 的 ①）。
代价：advisor 承担实时中转——ingest 通路独立线程/进程，与业务循环只经持久化队列交接（音频背压不得阻塞模型循环）。

### D3 鉴权是 Day 1 需求

advisor 是多用户唯一入口。准入凭据复用旧 recorder OAuth 的 `user_access_token`：advisor 校验 token 有效性、用户是所选日历事件的组织者、处于会议时间窗（语义照抄旧 stt-server v2，代码从旧模块平移）。stt-server / llm-gateway 保持内网无认证。

### D4 通知与订阅是 advisor 的一等子系统

bot 是 advisor 的子系统（同进程库表直读），不是独立服务。原 `recipients` 静态配置的等价物 = 订阅表中 `(session, state='approved')` 的集合：组织者行在会话建立时自动写入，小B 行经审批写入。每条决策 fan-out 落 outbox 多行（`local` 行恒存为事实来源 + 每收件人一行 IM 行），发送状态机、幂等 UUID、失败 uncertain 人工裁决——全部沿用旧 advisor outbox 语义。

### D5 可靠性语义平移，不降级

旧 spool 契约的幂等语义（event_id/sequence 去重、缺口等待、stream_end 确认、断线续传）原样搬进新的 recorder↔advisor 网络契约；advisor→gateway 的幂等 request_id 契约不动；一次 HTTP 断开 ≠ 任务失败/取消；迟到结果按规则丢弃或复核——旧 plan.md §3 的边界条目逐条继承，本文不重复。

## 4. 核心数据流与状态机

### 4.1 回答分发（S2/S3 主链路）

```
音频 → 字幕(final) → 会话上下文 → 模型任务(gateway) → 决策复核
  → outbox: 1 行 local（事实来源）+ N 行 im（组织者 + approved 小B，逐行独立状态机）
  → bot 逐行发送（uuid 幂等）→ sent / uncertain（人工裁决工具沿用）
```

订阅集合在**每次决策时**实时查询：审批通过即刻对下一条回答生效，撤销即刻止血（撤销时同时 cancel 该收件人的 pending IM 行）。

### 4.2 订阅状态机

```
requested ──小A同意──► approved ──小A撤销──► revoked
    │                                   ▲
    └──小A拒绝──► denied ──小B再申请──► requested(新)
仅 approved 收分发；仅前向，不回溯。
```

### 4.3 会话生命周期

```
recording ──停止采集/超时──► draining ──输入/任务/回答全部落定──► ended
ended：拒绝新订阅、迟到模型结果不应用（沿用旧语义）。
```

## 5. 契约清单（随实现冻结）

| 契约 | 两端 | 替代/来源 |
|---|---|---|
| recorder ↔ advisor 流式接入（WS） | 采集器 ↔ ingest 网关 | 新写；语义源自旧 CONTRACT_RECORDER_INPUT |
| STT 流式（WS） | advisor ↔ stt-server | 沿用旧 CONTRACT_STT_STREAM，auth 降为 none |
| 远程推理（HTTP） | advisor ↔ llm-gateway | 沿用旧 CONTRACT_REMOTE_INFERENCE，不变 |
| 飞书 IM 事件/发送 | bot ↔ 飞书开放平台 | 权限清单沿用旧 docs/APP_PERMISSIONS_EVENTS.md 已验证条目 |

## 6. 实施阶段（建议）

- **C0 冻结契约**：流式接入契约 + 订阅/审批 API 面；建模块骨架。
- **C1 单用户上云**：recorder 推流 → 字幕 → gateway（假模型）→ 决策落库；bot 上云，S1–S3 全场景端到端（含假飞书桩）。
- **C2 多租户**：按用户隔离、配额、订阅并发；真实 GLM 与真实飞书联调。
- **C3 收尾**：部署/监控/保留策略、旧仓库资产归档核对、真实会议验收取证。
