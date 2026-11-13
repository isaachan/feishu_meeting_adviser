# macos-gui 客户端设计（v1）

状态：2026-11-12 立项，**设计评审稿（未实现）**。本文是 `macos-gui` 的唯一工作依据；§5 的契约变更草案经评审后回写正式契约（bump v1.2/v0.5）再动代码。
上游：[ARCHITECTURE](../docs/ARCHITECTURE.md)、[DEPLOYMENT](../docs/DEPLOYMENT.md)、[CONTRACT_ADVISOR_API](../docs/CONTRACT_ADVISOR_API.md)、[CONTRACT_AUTH](../docs/CONTRACT_AUTH.md)、[CONTRACT_RECORDER_STREAM](../docs/CONTRACT_RECORDER_STREAM.md)。

## 1. 定位与用户流程

GUI 客户端 = recorder 的图形分发形态：**同样的采集与上行能力，零参数、零终端操作**。

用户流程（目标态）：

1. 小A 在飞书里对会参谋 bot 说「入会 xxxxxxx」（既有链路，不变）；
2. 打开 GUI（首次使用：一次性输入服务器地址 + 扫码登录飞书）；
3. GUI 后台轮询云端，会话列表出现该活跃会话（标题/会议号/开始时间/phase）；
4. 小A 从列表点选会话 →「开始采集」→ GUI 直接启动采集上行，**全程无参数输入**；
5. 监控面板展示上行状态；「停止采集」走干净收尾；关窗隐藏窗口、采集继续。

**非目标（v1）**：GUI 内入会/结束会议（仍走飞书 bot 指令）、托盘图标、暂停/恢复、Windows loopback（WASAPI 属 recorder 侧后续任务）。

## 2. 决策记录（2026-11-12 对齐）

| # | 决策 | 选择 |
|---|---|---|
| D1 | 云端通道 | advisor 新增 GUI 端点 `/gui/*`，经现有 HTTPS 反代域名暴露（不新建服务节点） |
| D2 | 身份方案 | 云端 device-flow 扫码：GUI 只获 **device_token**（设备凭据），user token 留在 auth，红线不破 |
| D3 | 进程模型 | 同进程：GUI 直接 import recorder 的 `CaptureSession`/`SourceUplink`（TCC 权限归 GUI，只弹一次；SCK Swift helper 子进程不变） |
| D4 | 技术栈 | PySide6（Qt）；跨平台代码，mac 先行打包 .app，Windows 同代码出 .exe |
| D5 | 能力边界 | 只做发现 + 确认 + 监控 + 停止采集 |
| D6 | 多会话 | 永远列表选择，**不做弹窗** |
| D7 | 关窗语义 | 采集中关窗 = 隐藏窗口继续采集（Dock 图标恢复）；无采集关窗 = 退出；Cmd-Q 采集中 = 确认「停止采集并退出？」→ 干净收尾后退出 |
| D8 | 托盘 | v1 不做 |
| D9 | 命名 | 目录 `macos-gui`（按需保留），Python 包名 `macos_gui` |

## 3. 架构

```
┌── 组织者 laptop ──────────────────────┐   ┌── 云端单机 ──────────────────────┐
│  macos_gui (PySide6, 同进程)          │   │  advisor                          │
│  ├─ ui/ 列表页·监控页·设置页·登录页    │   │  ├─ /gui/* ──(HTTPS 反代, 新)───┐ │
│  ├─ core api/login/poller/config      │◄─┼─┼─ login/start·poll·sessions·   │ │
│  ├─ capture (import recorder)         │   │  │  logout + devices 表          │ │
│  │   └─ SCK helper 子进程(Swift)      │   │  └─► auth /authorize/* /status  │ │
│  └─ keyring: server_url, device_token │   │  auth(凭据库, user token 不出云) │ │
│         │ ws://host:8400/ingest       │   └──────────────────────────────────┘ │
└─────────┴──────────────────────────────┘   （bot/ingest/stt/gateway 链路不变）
```

- 新增交互只有一条：GUI ↔ advisor `/gui/*`（HTTPS）。采集上行、入会、决策、通知全部沿用既有契约，零改动。
- 模块依赖纪律的显式例外：`macos_gui` **直接依赖 recorder 包**（它是 recorder 的 UI 壳，同属 laptop 侧分发形态）；对 advisor/auth 仍只走契约 HTTP。

## 4. 身份与登录（device-flow 扫码）

目的：识别 laptop 属于哪个飞书用户（open_id / name / email，如 "Kai Han (kai.han@nio.com)"），并让 GUI 后续查询持有可鉴权的设备凭据。**user_access_token / refresh_token 全程不出云**。

```
GUI                    advisor /gui                auth                     飞书
 │ POST /gui/login/start  │                          │                       │
 │───────────────────────►│── authorize/start ──────►│                       │
 │                        │  {open_id:null,          │                       │
 │                        │   intent:"gui:login"}    │  PKCE+state(discovery)│
 │◄─ login_url+device_code+expires_at ──┐            │                       │
 │ [显示 QR(=login_url) + 浏览器打开链接] │            │                       │
 │   手机飞书扫码 ─────────────────────────────────────────────────────────►│ 授权页
 │                        │                          │◄─ /oauth/callback ────┤
 │                        │                          │  换取+user_info       │
 │                        │                          │  凭据按 open_id 落库   │
 │ GET /gui/login/poll?device_code= (2~3s 间隔)      │                       │
 │───────────────────────►│── GET /authorize/status?state= ─►│              │
 │                        │◄─ completed{open_id,name,email} ─│              │
 │◄─ {device_token, user} ─│ devices 表落 sha256(token)      │              │
```

- **发现式登录**：auth 现契约要求 state 预绑定 open_id（防串换）。GUI 场景 open_id 恰是待发现项 → v1.2 允许 `intent:"gui:login"` 时 `open_id` 缺省，回调后 user_info 的 open_id **采纳**为主体；其他 intent 仍严格绑定校验。
- **额外收益**：扫码申请的 scopes 即日历读取 + offline_access，登录同时完成 OAuth 凭据托管——之后小A 说「入会」不会再收到授权链接。
- device_token：`dt-` 前缀 + ≥32B urlsafe 随机；advisor 只存哈希（devices 表：`token_sha256, open_id, created_at, last_seen_at`）；本地存 Keychain（Windows 走 Credential Manager，同 keyring 接口）。v1 无 TTL，退出登录/服务端清理即失效。
- 防冒领：领 token 需同时持有 `device_code`（仅在 GUI）与完成 OAuth 的 `state`（仅扫码者链路），两者均随机一次性、TTL 10 min。
- 顶栏常显身份 "Kai Han (kai.han@nio.com)"——用错账号时列表为空可自解释。

## 5. 云端契约变更草案（评审后回写正式契约）

### 5.1 CONTRACT_ADVISOR_API v1.2 —— 新增 §9 GUI 面

传输：经 TLS 反代 `https://<域名>/gui/*` → `127.0.0.1:8380`（与既有 localhost API 同进程、不同暴露面）。错误体沿用 `{"error":{code,message}}`。**响应不得含任何用户 OAuth 凭据**；`recorder_command_args` 白名单与附录 A 一致。

| 端点 | 请求 | 成功响应 | 错误 |
|---|---|---|---|
| `POST /gui/login/start` | `{}` | `201 {login_url, device_code, expires_at}` | `auth_unavailable` |
| `GET /gui/login/poll?device_code=` | — | `200 {status:"pending"}` 或 `{status:"completed", device_token, user:{open_id,name,email}}` | `404 unknown_device` / `410 expired` |
| `GET /gui/sessions` | `Authorization: Bearer dt-…` | `200 {user, sessions:[…]}`（见下） | `401 bad_token` |
| `POST /gui/logout` | 同上 | `200 {ok:true}`（幂等） | `401 bad_token` |

`GET /gui/sessions` 响应：

```json
{"user": {"open_id": "ou_A", "name": "Kai Han", "email": "kai.han@nio.com"},
 "sessions": [{"session_id": "s-3f9c", "meeting_no": "123456789", "title": "周会",
   "phase": "recording", "started_at": "…",
   "recorder_command_args": {"session": "s-3f9c", "event": "cal_x/evt_y",
     "url": "ws://server:8400/ingest", "token": "st-xxx", "mic": false}}]}
```

- 会话集：`recording`（含完整 args，可启动）+ `draining`（`recorder_command_args` 为 `null`，GUI 置灰展示"排空中"）；按 `started_at` 倒序。
- 流令牌暴露面说明：`st-xxx` 本就要下发 laptop 内存（与 bot 渲染命令给小A 同级受信面，v1 接受并记入 §9 风险表）。

### 5.2 CONTRACT_AUTH v1.2 —— 发现式登录与 state 查询

1. `POST /authorize/start`：`intent:"gui:login"` 时允许 `open_id` 缺省（state 记 `discovery=true`）；其余 intent 行为不变。
2. `GET /oauth/callback`：discovery state 换取后以 user_info 的 `open_id` 为主体落凭据（防串换校验仅跳过 discovery state）；成功后 state 记录补存 `user_info` 摘要 `{open_id, name, en_name, email}`。
3. 新增内部端点 `GET /authorize/status?state=`（仅 advisor 调用）→ `200 {status:"pending"|"completed", user?:{…}}`；不存在/TTL 过期 → `404 not_found`。completed 幂等可读，**不返回任何 token**。

### 5.3 DEPLOYMENT v0.5

- §2 组件表新增：advisor GUI 面 `https://<域名>/gui/*`（反代 → 127.0.0.1:8380），GUI/浏览器可达。对外入口由两个变**三个**（OAuth 回调、ingest WS、GUI API）。
- §3 凭据表新增：device_token（laptop Keychain 明文 + advisor devices 表哈希；非用户 OAuth 凭据，泄露影响面见 §9）。
- §1 节点表「组织者 laptop」承载由 `recorder` 更新为 `recorder / macos-gui（GUI 分发形态）`。

## 6. GUI 设计

### 6.1 模块结构

```
macos-gui/
  pyproject.toml          # 依赖: pyside6, keyring, recorder(本地 whl 路径依赖)
  src/macos_gui/
    __main__.py           # python -m macos_gui 入口
    config.py             # server_url/device_token 读写（keyring + QSettings 兜底）
    api.py                # /gui/* 客户端（stdlib urllib，超时/线性退避）
    login.py              # device-flow 控制器（QR pixmap、poll 循环、token 落库）
    sessions.py           # 会话轮询器（QTimer，空闲 10s / 采集中 5s）
    capture.py            # 组装 recorder 采集栈（QThread 内跑，Qt 信号上报 stats）
    app.py  ui/           # QApplication 与窗口（列表/监控/设置/登录四视图）
  tests/  README.md
```

### 6.2 客户端状态机

```
boot ──无 server_url──► settings ──保存──► login_qr
boot ──有──┬─无 device_token─► login_qr ──poll completed──► listing
           └─有 token────────► listing
listing ⇄ offline（网络失败：退避重试，不清 token；恢复自动回）
listing ──点选会话+开始采集──► capturing ──停止/会话 ended──► listing
任何 401 bad_token ──► login_qr（token 失效，重新扫码）
```

### 6.3 采集集成（capture.py）

- 复用 recorder：按 `recorder_command_args` 构造 `SwiftLoopbackSource`（+ `MicSource` 当 `mic:true`）→ `CaptureSession` → 每源 `SourceUplink`；重连/重传/gap/退出码语义全部继承（CONTRACT_RECORDER_STREAM v1 不变）。
- 上报：每 1–2s 快照每源 `sent/acked/buffered/reconnects/drops` + 连接状态 → Qt 信号 → 监控面板。
- 停止：UI「停止采集」→ 置停止事件 → 干净 `stream_end` → 等 `stopped`（有界超时）；Cmd-Q 采集中走 D7 确认。
- 会话侧结束：轮询见 `draining/ended` 或 uplink 收 `session_ended`/`bad_token` → 自动停止采集回列表，界面提示原因。
- 错误映射（uplink 退出码 → UI 文案）：2 →「会话已失效，请回飞书重新入会」；3 →「连接不可恢复」；130 → 视为手动中断回列表。

### 6.4 UI 骨架

- 列表页（主页）：顶栏身份 + 会话卡片（标题/会议号/开始时间/phase 徽标）+ [刷新]；draining 置灰；选中卡片 [开始采集]。
- 监控页：每源状态卡（连接状态/上行计数/缓冲/重连次数）+ [停止采集] + [返回列表]（返回不停止采集）。
- 登录页：QR + 「在浏览器打开」链接 + 等待状态；过期自动刷新 QR。
- 设置页：server URL 输入（一次性）。

## 7. 运行与分发形态

- **命令行形态（开发/验收先行）**：venv 安装 macos_gui（含 recorder 路径依赖）→ `python -m macos_gui` 或 console 入口 `macos-gui` 直接运行。SCK helper 首次使用 swiftc 现场编译（recorder 既有行为）。功能验收先在此形态完成。
- PyInstaller onedir `.app`（PySide6 + macos_gui + recorder 包 + 预编译 SCK helper；打包时 swiftc 编译 helper 放入 bundle，经 `LOCAL_MEETING_RECORDER_SCK_BIN` 指向——recorder 已支持该覆盖）。
- 不签名/不公证：README 引导右键打开绕 Gatekeeper；TCC 弹窗（屏幕录制=SCK、麦克风=开 mic 时）一次授权长期有效（TCC 记 .app）。
- Windows：同代码 `pyinstaller onedir .exe`；loopback 源缺失时启动采集显式报「该平台暂不支持内录」（mic 可用）——WASAPI loopback 属 recorder 后续任务（G7）。

## 8. 任务拆分

| # | 任务 | 范围 | 验收 |
|---|---|---|---|
| G1 | 契约回写 | §5 草案回写三份契约并 bump（ADVISOR_API v1.2 / AUTH v1.2 / DEPLOYMENT v0.5） | 评审通过，含时序示例 |
| G2 | auth 改动 | 发现式 start、回调落 user_info、`GET /authorize/status` | 单测：discovery 采纳主体/其他 intent 仍防串换/status 三态/不回 token |
| G3 | advisor GUI 面 | devices 表 + 四端点（login/start·poll·sessions·logout），args 组装复用建会逻辑 | 单测：冒充 bad_token/poll 冒领防护/draining 无 args/响应无用户凭据（红线断言） |
| G4 | GUI 骨架 | PySide6 壳 + 设置/登录/列表 + api/poller（对桩 server） | 桩模式登录→列表全流程；401 重扫码（命令行形态） |
| G5 | 采集集成 | capture.py + 监控页 + 停止/会话结束联动 + 关窗语义（D7） | 桩 ingest 端到端：发现→采集→ack 增长→干净停止；合盖/断网重连（命令行形态） |
| G6a | 命令行形态验收 | 干净 venv + `python -m macos_gui` 跑真实链路 | §10 阶段一取证 |
| G6b | 打包验收 | PyInstaller .app + 文档 + 干净 Mac 实测 | §10 阶段二取证（重点 TCC/Gatekeeper/打包差异） |
| G7 | Windows（后续） | recorder WASAPI loopback + win 打包 | 独立立项，不阻塞 G1–G6 |

## 9. 风险与显式接受项

| 项 | 评估 | 处置 |
|---|---|---|
| device_token 被盗 = 可领该用户活跃会话的流令牌（推音频入会话） | 与 ingest 明文 ws 同级受信面 | v1 受信网络接受；公网前补设备绑定/短 TTL/服务端吊销面 |
| 发现式登录放宽 state→open_id 绑定 | 仅 `intent:"gui:login"` 的 state；领 device_token 仍需 device_code 在手 | G1 评审固化；auth 单测双 intent 对照 |
| /gui/sessions 携流令牌过 HTTPS | 与 bot IM 下发命令同级 | 记入契约 §9 风险表 |
| PyInstaller + PySide6/sounddevice 共存打包 | 体积 ~100–200MB；PortAudio 由 sounddevice wheel 自带 | G6 实测；v1 手动整包替换升级 |
| Qt 与 recorder 线程模型合流 | CaptureSession/uplink 自带线程；Qt 只做 UI 与编排 | capture 全部在 QThread/信号面隔离，不共线程 |
| 崩溃兜底 | GUI 崩溃 = 采集中断（同进程代价） | recorder 的断线缓冲在进程内失效属接受项；云端 idle_timeout→draining 兜底（既有语义） |

## 10. 验收口径（G6a 命令行形态 → G6b 打包形态）

**阶段一（G6a，命令行）**：干净 venv 安装 → `python -m macos_gui` → 设置页输入服务器地址 → 扫码登录（顶栏出现身份）→ 飞书发「入会」→ 列表 ≤10s 出现会话 → 开始采集 → 监控计数增长 → bot 收到决策通知 → 关窗采集继续（Dock 恢复）→ 停止采集干净收尾（`stopped` 确认）→ 飞书「结束会议」→ 会话 ended、GUI 自动回列表。

**阶段二（G6b，打包 .app）**：干净 Mac 装 .app → 右键打开（Gatekeeper 引导）→ TCC 授权一次 → 重复阶段一关键路径（登录/发现/采集/干净停止/关窗继续）；对比确认打包形态无行为差异。

取证存 `../data/acceptance-gui/`，按 `cli/`、`app/` 分目录。
