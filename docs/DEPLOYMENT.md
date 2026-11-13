# 部署 · 节点与基线（v0.5 草案）

状态：设计基线。组件职责见 [ARCHITECTURE](ARCHITECTURE.md)。
修订：v0.3 将 bot 与 OAuth 鉴权服务从 advisor 拆出，连同 advisor 共三个**独立部署节点**（v0.2 曾为 advisor 子系统）；v0.4 评审修订：v1 最简网络基线——后端不接公网，ingest 走受信网络明文 ws，auth 内部 API 仅 advisor 调用（bot 不直连）；**v0.5（2026-11-12）**：advisor 新增第三个对外入口 `/gui/*`（HTTPS 反代，macos-gui 客户端用，CONTRACT_ADVISOR_API §8），组织者 laptop 承载增加 macos-gui，凭据面新增 device_token。

## 1. 部署节点总览

| 节点 | 数量（v1） | 承载 | 说明 |
|---|---|---|---|
| 云端服务器 | 1（单机） | **五个独立部署节点**：`advisor`（会话核心）、`bot`（IM 控制面/通知面）、`auth`（OAuth 鉴权服务），及 `stt-server`、`llm-gateway` | Linux（Ubuntu）；CPU 即可（STT int8 模型已在旧部署验证）。三节点单机同驻但**进程/配置/数据/升级完全独立**，未来可拆机部署 |
| 组织者 laptop | 每组织者一台 | 飞书客户端、recorder / macos-gui（GUI 分发形态，2026-11-12 立项） | Mac 先行、Windows 跟进；安装包不签名/公证，文档引导绕过 Gatekeeper/SmartScreen；**零配置零凭据**（GUI 仅一次性服务器地址 + device_token 设备凭据），按 IM 下发命令（recorder CLI）或 GUI 会话发现启动 |
| 参会者设备 | 任意 | 飞书客户端 | 零安装、零配置 |
| 飞书云（外部） | — | 自建应用（IM 长连接、日历/OAuth 授权页） | SaaS；应用后台需预先注册 OAuth 回调 URI（见 §2） |

三个核心节点的职责切分：

| 节点 | 职责 | 不承担 |
|---|---|---|
| `advisor` | ingest 网关（音频/事件上行）、会话域（字幕/材料/任务/outbox/订阅）、会话监督器、组织者/时间窗校验 | 不持有用户 OAuth 凭据（经 auth API 取）；不碰飞书 IM |
| `bot` | 飞书长连接收发、指令解析、订阅审批交互、outbox 待发 IM 的拉取与回写 | 不落业务状态（仅 IM 去重等运行时态）；业务操作全经 advisor 内部 API |
| `auth` | 授权链接/回调/换取/刷新/吊销、用户 OAuth 凭据保管（按 open_id 隔离） | 不做业务校验（组织者/时间窗校验在 advisor，用 auth 提供的 token 读日历） |

## 2. 组件与端口基线

| 组件 | 监听 | 暴露范围 | 说明 |
|---|---|---|---|
| auth OAuth 回调（HTTPS） | `https://<域名>/oauth/callback`（反代 → `127.0.0.1:8381`） | 浏览器可达（部署网络内，**含手机**点开 IM 里的授权链接；域名可仅内网/VPN 解析） | 首次授权/重新授权；redirect URI 必须与飞书应用后台注册值**精确匹配** |
| auth API（HTTP） | `127.0.0.1:8381` | 仅 localhost | advisor 获取有效用户凭据（`get_valid_token(open_id)` 类接口）与授权 URL；bot 不直连 auth |
| advisor ingest（WS） | `0.0.0.0:8400` | 受信网络 | recorder 音频/事件上行唯一入口；**流令牌**鉴权（非用户凭据）；每条音频源一条连接；v1 受信网络**明文 ws**（最简基线，公网/拆机前补 TLS） |
| advisor API（HTTP） | `127.0.0.1:8380` | 仅 localhost | bot 调用（指令触发会话操作、订阅读写、待发 IM 拉取/回写）+ 运维 CLI |
| advisor GUI 面（HTTPS，v0.5） | `https://<域名>/gui/*`（反代 → `127.0.0.1:8380`） | 受信网络内 GUI/浏览器可达 | macos-gui 登录（device-flow）与活跃会话发现（CONTRACT_ADVISOR_API §8）；**device_token** 鉴权（非用户凭据）；与 auth OAuth 回调共用反代域名 |
| bot | 出站长连接 | — | 主动外连飞书（`lark.ws.Client`），不监听端口 |
| stt-server（WS） | `127.0.0.1:8392`（沿用旧部署） | 仅 localhost | auth 固定 `none`（准入已由 advisor 完成） |
| llm-gateway（HTTP） | `127.0.0.1:8377`（沿用） | 仅 localhost | 与旧部署一致 |

网络边界一句话：**对外三个入口——auth 的 OAuth 回调（浏览器可达，HTTPS 域名经反代）、advisor 的 ingest WS（受信网络）与 advisor 的 GUI 面 `/gui/*`（HTTPS 经同一反代域名，v0.5）**；云内互调全部 localhost：bot→advisor、advisor→auth、advisor→stt-server、advisor→llm-gateway。

## 3. 凭据与配置

| 凭据 | 存放 | 用途 |
|---|---|---|
| 飞书自建应用 app_id/secret | `bot` 与 `auth` 各持一份所需配置（同一自建应用；bot 用于 IM 收发/tenant token，auth 用于 OAuth 换取），600 权限 | 两节点各自所需的最小能力 |
| 组织者 user_access_token/refresh_token | **`auth` 节点**（按 open_id 隔离存储；0600+独立数据目录，静态加密可选） | 凭据生命周期由 auth 管理；advisor 经 auth API 取有效 token 用后不留存（bot 不直连 auth） |
| 会话流令牌 | `advisor` 铸造 → bot 经 IM 命令下发 **或 GUI `/gui/sessions` 下发** → laptop 内存 | 仅绑定单一 session、短期、会话结束即吊销；**任何用户凭据不落 laptop** |
| GUI device_token（v0.5） | laptop Keychain（明文）+ `advisor` devices 表（sha256 哈希） | 设备凭据，**非用户 OAuth 凭据**；扫码登录（device-flow，CONTRACT_AUTH v1.2 discovery）铸造；无 TTL（手动退出/服务端清理失效）；泄露影响面=可领该用户活跃会话的流令牌，与 ingest 明文 ws 同级受信面 |
| GLM 凭据 | `llm-gateway` 运行环境（沿用旧 OpenCode 登录注入） | 模型执行 |
| 节点间（云内） | 无 | localhost 互调无认证（同机受信边界；拆机部署时补 mTLS/服务间凭据） |

v1 运营在受信网络（内网/VPN），后端不接公网：ingest 走明文 ws（v1 接受），OAuth 回调经 TLS 反代（域名只需在部署网络内解析）。接入公网或三节点拆机前必须补：ingest TLS、服务间认证、用户凭据静态加密、按用户配额与隔离审计。

## 4. 数据落点

| 数据 | 位置 | 保留 |
|---|---|---|
| 会议字幕/材料/决策/订阅/outbox | `advisor` 数据库（按用户隔离） | 按策略（v1 默认 7 天沿用旧 retention，到期清字幕保决策摘要） |
| 用户 OAuth 凭据 | `auth` 数据目录（按 open_id 隔离） | refresh 失效→要求重新授权；删除用户=连凭据一并删 |
| 模型任务明细 | `llm-gateway` | 终态 24h 过期清除（沿用旧契约） |
| 整场原始音频 | **laptop 本地**（不上传） | 不自动删除，手动清理 |
| 断线音频缓冲 | laptop 本地（内存/临时文件） | 重传成功即清 |
| 流令牌 | laptop 内存 | 进程退出即失；会话结束 advisor 即吊销 |

bot 无业务持久状态（重启丢失的仅 IM 去重缓存，飞书事件重投由 message_id 幂等吸收）。

## 5. 运维基线（v1）

- 部署前置：飞书应用后台注册 OAuth 回调 URI（与 §2 精确一致，指向 auth 节点）、开通 IM 三权限与长连接事件（沿用旧 docs/APP_PERMISSIONS_EVENTS.md 清单）。
- advisor 节点前置：安装 noam CLI——NSDP 版本计划取数经 `noam nsdp` 子进程（token 仅经子进程环境传递，见 CONTRACT_ADVISOR_API §1.1 / CONTRACT_AUTH §8）；不在 PATH 时以 `--noam-bin` 指定路径。缺失不阻断启动（启动时探测并记 ERROR 日志），但 NSDP 取数将持续降级为无版本计划材料。
- 进程管理：systemd 五个独立单元（`advisor`、`bot`、`auth`、`stt-server`、`llm-gateway`），各自有界停机（advisor 需 draining 会话排空后再退出；bot/auth 可直接重启）。
- 独立升级：三核心节点互不牵连——bot 短暂下线仅审批/通知延迟，字幕与模型链路不受影响；auth 短暂下线仅影响新授权/校验，进行中会话不受影响；advisor 升级前确认无 draining 会话。
- 健康检查：各节点 healthz；最小指标：ingest 连接数/上行速率/积压、STT 会话数、gateway 队列深度、活跃会话数、bot 长连接状态。
- 凭据运维：auth 凭据存储的访问审计；备份包含凭据时需加密。
- 单机单点：服务器宕机 = 所有进行中会议的辅助中断（音频本地留存仍在）；不做高可用（v1 明确接受）。
- 契约变更需 bump 版本并保证一版向前兼容（含三节点间的内部 API）。
- 备份：advisor 数据库与 auth 凭据目录按用户可导出/删除（合规预留，v1 手工脚本）。

## 6. 首版验收环境

单台 Ubuntu 服务器（OAuth 回调域名 + TLS 反代，域名可仅内网/VPN 解析；五个 systemd 单元）+ 一台 Mac laptop + 两个飞书账号（小A/小B），覆盖 REQUIREMENTS S1–S5 全场景：含首次授权全流程（电脑与手机各点一次授权链接，回调落在 auth 节点）、四种拒绝路径（未授权/非组织者/超窗/会议不存在）、断网/合盖/撤销/拒绝、bot 与 auth 各自重启不影响进行中会话；模型先用 gateway 假模型打通，再切真实 GLM（沿用旧仓库"假模型先行"验收方法）。
