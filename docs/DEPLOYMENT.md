# 部署 · 节点与基线（v0.5 草案）

> **生产实施更新（2026-11-16）**：服务器为 Ubuntu `10.132.67.11`，对外域名 `meeting-advisor.nioint.com`；OAuth、提交页面、GUI 和 admin-site 暂用 HTTP/域名，运维台通过 `http://meeting-advisor.nioint.com/admin` 访问（登录功能后续增加）。systemd 管理六个服务，具体操作以本文末 §7 为准；下文原 HTTPS、五个服务的设计基线在首期以此更新为准。

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
| auth token 提交（HTTPS） | `https://<域名>/nsdp/submit`、`/jira/submit`（反代 → `127.0.0.1:8381`） | 浏览器可达 | 一次性链接；仅公开这两个表单路径，不公开内部凭据 API |
| auth API（HTTP） | `127.0.0.1:8381` | 仅 localhost | advisor 获取有效用户凭据（`get_valid_token(open_id)` 类接口）与授权 URL；bot 不直连 auth |
| advisor ingest（WS） | `0.0.0.0:8400` | 受信网络 | recorder 音频/事件上行唯一入口；**流令牌**鉴权（非用户凭据）；每条音频源一条连接；v1 受信网络**明文 ws**（最简基线，公网/拆机前补 TLS） |
| advisor API（HTTP） | `127.0.0.1:8380` | 仅 localhost | bot 调用（指令触发会话操作、订阅读写、待发 IM 拉取/回写）+ 运维 CLI |
| advisor GUI 面（HTTPS，v0.5） | `https://<域名>/gui/*`（反代 → `127.0.0.1:8380`） | 受信网络内 GUI/浏览器可达 | macos-gui 登录（device-flow）与活跃会话发现（CONTRACT_ADVISOR_API §8）；**device_token** 鉴权（非用户凭据）；与 auth OAuth 回调共用反代域名 |
| bot | 出站长连接 | — | 主动外连飞书（`lark.ws.Client`），不监听端口 |
| stt-server（WS） | `127.0.0.1:8392`（沿用旧部署） | 仅 localhost | auth 固定 `none`（准入已由 advisor 完成） |
| llm-gateway（HTTP） | `127.0.0.1:8377`（沿用） | 仅 localhost | 与旧部署一致 |

网络边界一句话：**对外三个入口——auth 的 OAuth 回调（浏览器可达，HTTPS 域名经反代）、advisor 的 ingest WS（受信网络）与 advisor 的 GUI 面 `/gui/*`（HTTPS 经同一反代域名，v0.5）**；云内互调全部 localhost：bot→advisor、advisor→auth、advisor→stt-server、advisor→llm-gateway、受控查询工具→auth。

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
- **状态机 v2 运行参数（2026-11-17，SM-13）**：advisor `serve` 新增可配置预算——`--observe-poll-s`（owner 在会轮询间隔，默认 15）、`--observe-timeout-s`（单次观测调用超时，默认 10）、`--max-observation-age-s`（快照时效，默认 60）、`--approval-ttl-s`（订阅审批等待，默认 1800）、`--drain-deadline-s`（排空总截止，默认 600）、`--reply-validity-s`（回答有效期，默认 3600）。除无音频 900 秒外均为工程默认值（STATE_MACHINES v2 §5），生产按真实耗时校准。用户 OAuth 请求 scope 新增用户版 `vc:meeting.meetingevent:read`（CONTRACT_AUTH v1.6）：存量凭据不受影响，缺该 scope 的用户由 advisor 经 outbox 一次性提醒重新授权（会话观测降级为 unknown，不伪造离会）。
- **富文本回答（2026-11-22，T17）**：advisor 默认 `--decision-output decision-v2`，bot 默认 `--rich-text` 开启（`BOT_RICH_TEXT=0` 或 `--no-rich-text` 关闭）；关闭时 Markdown 按纯文本显示。回滚：`advisor --decision-output decision-v1` 和/或 `bot --no-rich-text`；bot 渲染失败自动回退纯文本。方言与安全边界见 [FEISHU_RICH_TEXT_REPLY](FEISHU_RICH_TEXT_REPLY.md)。
- **schema 升级**：advisor 数据库自动按 v4→…→v8 链式迁移（STATE_MACHINES v2 §6：实例键/claims/观测列/审批 deadline/回答有效期），升级前备份 adviser.sqlite3；回滚不可逆，靠备份恢复。admin-site 的「小C 名单」是 owner 权限面：仅受信网络使用（现状 localhost/内网域名），扩大暴露面前必须先加认证。
- llm-gateway 节点前置：安装 noam CLI；使用 `--noam-bin` 指定路径、`--auth-url` 指定受信 auth 服务，正式 skill 根目录用 `--skills-dir` 指定。NSDP 查询按需执行，token 仅经查询子进程环境传递；缺少 noam 或 auth 不可达时工具返回失败。advisor 与 gateway 的身份扩展须同步部署。
- Jira 查询：在 gateway 配置 `--jira-url https://<Jira Data Center 地址>` 或环境变量 `JIRA_URL`，确保服务器可访问该地址；地址必须为 HTTPS。组织者按通知通过 `/jira/submit` 提交个人 PAT，工具只对 noam 子进程注入 PAT 并启用 `JIRA_READ_ONLY=true`。未配置地址时 Jira 查询不可用。auth、advisor、gateway 须同步部署。
- 进程管理：systemd 五个独立单元（`advisor`、`bot`、`auth`、`stt-server`、`llm-gateway`），各自有界停机（advisor 需 draining 会话排空后再退出；bot/auth 可直接重启）。
- 独立升级：三核心节点互不牵连——bot 短暂下线仅审批/通知延迟，字幕与模型链路不受影响；auth 短暂下线仅影响新授权/校验，进行中会话不受影响；advisor 升级前确认无 draining 会话。
- 健康检查：各节点 healthz；最小指标：ingest 连接数/上行速率/积压、STT 会话数、gateway 队列深度、活跃会话数、bot 长连接状态。
- 凭据运维：auth 凭据存储的访问审计；备份包含凭据时需加密。
- 单机单点：服务器宕机 = 所有进行中会议的辅助中断（音频本地留存仍在）；不做高可用（v1 明确接受）。
- 契约变更需 bump 版本并保证一版向前兼容（含三节点间的内部 API）。
- 备份：advisor 数据库与 auth 凭据目录按用户可导出/删除（合规预留，v1 手工脚本）。

## 6. 首版验收环境

单台 Ubuntu 服务器（OAuth 回调域名 + TLS 反代，域名可仅内网/VPN 解析；五个 systemd 单元）+ 一台 Mac laptop + 两个飞书账号（小A/小B），覆盖 REQUIREMENTS S1–S5 全场景：含首次授权全流程（电脑与手机各点一次授权链接，回调落在 auth 节点）、四种拒绝路径（未授权/非组织者/超窗/会议不存在）、断网/合盖/撤销/拒绝、bot 与 auth 各自重启不影响进行中会话；模型先用 gateway 假模型打通，再切真实 GLM（沿用旧仓库"假模型先行"验收方法）。

## 7. Ubuntu 生产部署操作

### 7.1 前置条件和配置

- 服务器 `10.132.67.11`，代码放在部署用户的 `$HOME/feishu_meeting_advisor`。以该用户执行下列命令；需有 sudo/systemd 权限。六个服务使用该 Linux 用户运行，因而可使用该用户已有的 noam、OpenCode 登录凭据和模型文件。
- 安装 Python 3.11+、`python3-venv`、pip 所需构建环境、curl、git 和 nginx；服务器需能安装各模块 Python 依赖。确认模型目录、noam 和 OpenCode 在该用户下可用。OpenCode 默认读取该用户的 `~/.local/share/opencode/auth.json`，也可通过 `OPENCODE_AUTH` 指定。
- 飞书后台 OAuth 回调设置为 **`http://meeting-advisor.nioint.com/oauth/callback`**，需与 `PUBLIC_BASE_URL` 拼出的地址完全一致。确保浏览器及手机可访问服务器端口 80，laptop 可访问 8400；admin-site 经 nginx `/admin` 访问，无需对外开放 8390。
- 拷贝并编辑配置（填写真实的飞书凭据及三个绝对路径）：

```bash
cp scripts/prod.env.example scripts/prod.env
chmod 600 scripts/prod.env
```

`scripts/prod.env` 是 shell 格式，仅保存可信的配置内容，不提交 Git。按需设置 `JIRA_URL`、`OPENCODE_AUTH`、`SKILLS_DIR`。将已有 `prod.env` 的 `PUBLIC_BASE_URL`、`INGEST_URL` 改为域名值（参见 `prod.env.example`）；admin-site 固定监听 localhost，旧的 `ADMIN_HOST` 可删除。默认真实模型 `REMOTE_BACKEND=opencode`；如需先验收假模型，可暂设 `fake`。GUI 的服务器地址填写 `http://meeting-advisor.nioint.com`。

### 7.2 部署、停止与升级

```bash
scripts/prod-up.sh                 # 首次部署或停机后更新
scripts/prod-down.sh               # 有 recording/draining 会话时拒绝停机
scripts/prod-down.sh --drain       # 结束会话并等待排空，默认最多 300 秒
scripts/prod-down.sh --drain --timeout 600
```

`prod-up.sh` 要求服务已停止且 Git 已跟踪文件无未提交变更；创建六个独立虚拟环境 `.venv-prod/<组件>`、重新安装组件，生成并启用 `meeting-advisor@<组件>.service`，安装 nginx 规则，按依赖顺序启动并检查健康状态。bot 无健康端口，脚本只能确认进程存活，飞书连接需结合日志和消息实测。部署成功后 `data/prod/deployed-commit` 保存 Git commit。若中途失败，检查日志并运行 `prod-down.sh` 后重试。

`--drain` 调用 advisor 运维 API 结束现有会话，保持 bot 运行以投递 outbox，并等待会话排空；然后停止 bot、复查会话，再按相反顺序停止服务。超时或请求失败时保留服务运行；停止 bot 后复查如发现新会话，则恢复 bot 并报错。排空依赖 STT、gateway、bot 的在途任务和 outbox；遇到无法排空的会话应排查原因。不要在有活动会话时直接 `systemctl stop` advisor。停止服务不会删除数据，也不会停止 nginx。

升级：先 `prod-down.sh`（必要时带 `--drain`），更新到目标 Git commit，再 `prod-up.sh`。回退同样先停服务，切回已验证的旧 commit 再运行 `prod-up.sh`；若涉及数据库结构变化，须同时评估并恢复兼容的数据备份。每个服务均可通过 `sudo systemctl restart meeting-advisor@bot` 等命令单独重启；单独重启 advisor 之前同样需要确认没有活跃或排空中的会议。

### 7.3 地址、日志与版本

| 入口/服务 | 监听与访问 |
|---|---|
| nginx | `80`；仅转发 `/oauth/callback`、`/nsdp/submit`、`/jira/submit`、`/gui/` 和 `/admin/`；`/admin` 重定向到 `/admin/` |
| advisor ingest | `0.0.0.0:8400`；默认下发 `ws://meeting-advisor.nioint.com:8400/ingest` |
| admin-site | `127.0.0.1:8390`（nginx 反代）；浏览器 `http://meeting-advisor.nioint.com/admin`，当前无登录 |
| auth、advisor API、STT、STT health、gateway | 分别为 localhost 的 `8381`、`8380`、`8392`、`8393`、`8377` |

数据在 `data/prod/`，日志在 `data/prod/logs/`，admin-site 从该目录读取五个服务日志。服务状态也可用 `systemctl status meeting-advisor@advisor` 查看；启动器故障可用 `journalctl -u meeting-advisor@advisor` 排查。日志文件需要按服务器运维策略定期轮转；备份 advisor 数据库和 auth 凭据时先停服务，并保护备份文件。

各服务初始版本均为 `0.1.0`，版本唯一来源为对应组件的 `pyproject.toml`。修改组件时递增其版本，提交后重新部署；`prod-up.sh` 会打印每个服务的已安装版本，也可执行例如 `.venv-prod/advisor/bin/advisor --version`。`deployed-commit` 与版本一起用于定位部署代码。

切换域名时，修改已有 `scripts/prod.env` 的 `PUBLIC_BASE_URL` 和 `INGEST_URL`，同步更新飞书回调配置及 GUI 服务器地址，停机后重新部署；需要 HTTPS 时还需配置 nginx TLS 和证书，并同步更新客户端地址。
