# 部署 · 节点与基线（v0.1 草案）

状态：设计基线。组件职责见 [ARCHITECTURE](ARCHITECTURE.md)。

## 1. 部署节点总览

| 节点 | 数量（v1） | 承载 | 说明 |
|---|---|---|---|
| 云端服务器 | 1（单机） | advisor（含 bot）、stt-server、llm-gateway | Linux（Ubuntu，沿用旧 stt-server/llm-gateway 的部署经验与 systemd 单元形态）；CPU 即可（STT int8 模型已在旧部署验证） |
| 组织者 laptop | 每组织者一台 | 飞书客户端、recorder | Mac 先行、Windows 跟进（沿用旧阶段 5 的双平台采集基线）；安装包不签名/公证，文档引导绕过 Gatekeeper/SmartScreen |
| 参会者设备 | 任意 | 飞书客户端 | 零安装、零配置 |
| 飞书云（外部） | — | 自建应用（IM 长连接、日历/OAuth） | SaaS，无部署动作 |

## 2. 云端组件与端口基线

| 组件 | 监听 | 暴露范围 | 说明 |
|---|---|---|---|
| advisor ingest（WS） | `0.0.0.0:<P_ingest>`（待定） | 受信网络 | recorder 音频/事件上行唯一入口；user_access_token 鉴权 |
| advisor API（HTTP） | 同机 | localhost | 管理/查询面（CLI、状态、订阅管理）；bot 同库直读不走走 API |
| advisor bot | 出站长连接 | — | 主动外连飞书（`lark.ws.Client`），不监听端口 |
| stt-server（WS） | `127.0.0.1:<P_stt>` | 仅 localhost | auth 固定 `none`（准入已由 advisor 完成） |
| llm-gateway（HTTP） | `127.0.0.1:8377`（沿用） | 仅 localhost | 与旧部署一致 |

网络边界一句话：**对受信网络只暴露 advisor ingest 一个端口，其余全部 localhost**。

## 3. 凭据与配置

| 凭据 | 存放 | 用途 |
|---|---|---|
| 飞书自建应用 app_id/secret | 云端 advisor 配置（600 权限） | bot 收发 IM、tenant token |
| 组织者 user_access_token/refresh_token | laptop recorder（沿用旧 OAuth 本地存储：0700 目录、原子写、只存散列身份） | 上行鉴权；advisor 侧校验后不留存 |
| GLM 凭据 | 云端 llm-gateway 运行环境（沿用旧 OpenCode 登录注入） | 模型执行 |
| 服务器间 | 无 | stt/gateway 内网无认证（沿用旧基线） |

v1 运营在受信网络（内网/VPN）；接入公网前必须补：ingest 通道 TLS、按用户配额与隔离审计（旧仓库根 TODO 的同款条目在此继承并扩大到 advisor）。

## 4. 数据落点

| 数据 | 位置 | 保留 |
|---|---|---|
| 会议字幕/材料/决策/订阅 | 云端 advisor 数据库（按用户隔离） | 按策略（v1 默认 7 天沿用旧 retention，到期清字幕保决策摘要） |
| 模型任务明细 | 云端 llm-gateway | 终态 24h 过期清除（沿用旧契约） |
| 整场原始音频 | **laptop 本地**（不上传） | 不自动删除，手动清理 |
| OAuth 凭据 | laptop 本地 | refresh 失效即要求重新授权 |
| 断线音频缓冲 | laptop 本地（内存/临时文件） | 重传成功即清 |

## 5. 运维基线（v1）

- 进程管理：systemd 单元（`advisor`、`advisor-bot` 可并入同单元、`stt-server`、`llm-gateway`），有界停机（draining 会话排空后再退出）。
- 健康检查：各组件 healthz；ingest 连接数/上行速率/积压、STT 会话数、gateway 队列深度四项最小指标。
- 单机单点：服务器宕机 = 所有进行中会议的辅助中断（音频本地留存仍在）；不做高可用（v1 明确接受）。
- 升级：组件独立升级；advisor 升级前确认无 draining 会话；契约变更需 bump 版本并保证一版向前兼容。
- 备份：advisor 数据库按用户可导出/删除（合规要求预留，v1 手工脚本）。

## 6. 首版验收环境

单台 Ubuntu 服务器 + 一台 Mac laptop + 两个飞书账号（小A/小B），覆盖 REQUIREMENTS S1–S5 全场景与断网/合盖/撤销/拒绝路径；模型先用 gateway 假模型打通，再切真实 GLM（沿用旧仓库"假模型先行"验收方法）。
