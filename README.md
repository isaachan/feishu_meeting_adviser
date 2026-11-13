# 会参谋（方案 C）

状态：**活跃开发中**。本仓库是新架构的唯一开发入口；旧架构（方案 B）的代码与文档在 [`legacy/`](legacy/) 目录，作为移植参考资产，不再维护。

## 这是什么

会参谋是一个实时会议 AI 助理：在会议进行中为**组织者**听会、判断并给出回答，并按组织者的显式授权，把回答分发给**参会者**。

新架构（方案 C）相对旧架构（方案 B，见 `legacy/plan.md`）的关键翻转：

| | 方案 B（旧，已停止维护） | 方案 C（本仓库） |
|---|---|---|
| 会议业务状态（会话/字幕/材料/决策） | laptop 本地 SQLite | 云端 advisor，集中多用户 |
| 通知 bot | 搁置（本地架构下无法闭环） | 云端独立部署节点（bot 服务），一等公民 |
| laptop 上跑什么 | recorder + advisor | 只跑飞书客户端 + local-recorder（音频上行） |
| 参会者获得信息 | 不支持 | 飞书 IM 订阅，经组织者审批 |

## 文档

- [产品需求（REQUIREMENTS）](docs/REQUIREMENTS.md)：personas（小A/小B）、端到端 scenarios、范围与非目标。
- [架构（ARCHITECTURE）](docs/ARCHITECTURE.md)：节点拓扑、模块职责、边界决策、数据流、关键状态机。
- [部署（DEPLOYMENT）](docs/DEPLOYMENT.md)：部署节点、组件端口、网络与凭据边界、首版基线。
- [实施计划（plan）](plan.md)：阶段任务拆解、依赖、里程碑（M1–M4）、验收口径与风险清单——团队工作依据。

## 模块规划（目标形态）

```
.
├── docs/                  # 本设计文档
├── recorder/              # laptop 端采集器（自旧 local-meeting-recorder 瘦身移植）
├── advisor/               # 云端会话核心（自旧 advisor 服务化改造：会话域/ingest/监督器）
├── bot/                   # 会参谋 bot 独立节点（IM 控制面/通知面）
├── auth/                  # OAuth 鉴权服务独立节点（用户凭据托管）
├── stt-server/            # 流式 STT 服务（自旧 stt-server 平移，退化纯内网）
├── llm-gateway/           # 模型执行网关（自旧 llm-gateway 平移）
├── macos-gui/             # laptop 端 GUI 客户端
├── integrations/          # 跨模块集成测试床
├── scripts/               # 本地全栈起停（local-up.sh / local-down.sh）
└── data/                  # 运行时数据（不入库）
```

每个模块独立配置、环境、测试与 `pyproject.toml`；模块间只经书面契约（WebSocket / HTTP）交接，不导入彼此源码——沿用已验证的模块独立原则。

## 从方案 B（legacy/）平移时的资产地图

| 旧资产 | 去向 |
|---|---|
| recorder 双路采集（SCK loopback + mic）、MuteGate、本地音频留存 | `recorder/`，去 spool 与本地 OAuth，改 WS 上行（流令牌） |
| 旧 recorder 的 OAuth 凭据管理（oauth.py：授权/换取/刷新/存储） | `auth/`，redirect URI 云端化，凭据按 open_id 云端保管 |
| 旧 recorder 的选会/组织者校验（feishu_calendar.py） | `advisor/`（用 auth 提供的 token 执行校验） |
| advisor 的 snapshot / decision / jobs / outbox / 会话状态机 | `advisor/` 核心域，基本平移，加多租户外壳与 ingest 网关 |
| 旧 spool/ack 契约的幂等语义（event_id/sequence/stream_end） | 演化为 recorder↔advisor 的网络契约 |
| stt-server（sherpa 流式 + v2 组织者授权） | `stt-server/`；鉴权前移到 advisor 后转内网模式 |
| llm-gateway（OpenCode/GLM 沙箱执行） | `llm-gateway/`，原样平移 |
| chatbot 的飞书长连接接线 | `bot/`（独立节点，经 advisor 内部 API 交互） |
