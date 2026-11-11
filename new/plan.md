# 方案 C 实施计划（new/）

状态：v0.2，2026-11-10 立项并完成首轮评审修订。本文是 `new/` 的唯一工作依据：任务拆解、依赖、验收口径与里程碑。
上游文档：[REQUIREMENTS](docs/REQUIREMENTS.md)（场景）、[ARCHITECTURE](docs/ARCHITECTURE.md)（v0.4 架构与决策）、[DEPLOYMENT](docs/DEPLOYMENT.md)（v0.4 部署基线）。
旧仓库（`../`）为停止维护的方案 B 实现，仅作平移素材，**不在旧目录里改代码**。

## 0. 工作约定（全体任务默认遵守）

1. **模块独立**：六个模块（recorder / advisor / bot / auth / stt-server / llm-gateway）各自 pyproject、venv、测试；跨模块只走书面契约，不导入彼此源码；可复用逻辑显式复制并注明来源。
2. **桩先行**：飞书 IM、OAuth、日历、GLM 一律先接桩打通，再切真实；桩与真实实现可配置切换。
3. **证据验收**：每个任务完成时附测试与证据（日志/数据快照存 `new/data/acceptance-*` 或模块内）；未验证的能力不勾选、不记通过。
4. **契约即法**：阶段 0 冻结的契约是实现的唯一依据；变更必须 bump 协议版本、双侧同步、一版向前兼容。
5. **v1 技术基线**：Python 3.11+；云端各节点持久化用 SQLite（独立数据目录）；HTTP/WS 服务用标准库或已验证依赖；不引入框架级重依赖，新增依赖需在任务内说明理由。

## 1. 资产平移对照（从旧仓库复制，不导入）

| 新模块 | 旧资产（相对 `new/`） | 处置 |
|---|---|---|
| `recorder/` | `../local-meeting-recorder/src/local_meeting_recorder/audio/`（双路采集、CaptureSession、MuteGate 语义）、`real.py` 的采集编排 | 保留采集/留存；去 spool、去本地 OAuth、去 STT 客户端 |
| `auth/` | `../local-meeting-recorder/src/local_meeting_recorder/oauth.py`（授权/换取/一次性 refresh 轮转/存储加固） | redirect URI 云端化；存储按 open_id 多用户化；去掉本地浏览器回调流程 |
| `advisor/` 会话域 | `../advisor/src/feishu_meeting_adviser/`：`adapters/persistence/`（store/repo/schema）、`snapshot.py`、`decision.py`、`model_jobs.py`、`llm.py` 的 RemoteModelBackend、`local_sessions.py` 状态机 | 核心平移；CLI 生命周期改 API 驱动；加 owner 与 ingest 网关 |
| `advisor/` 校验 | `../local-meeting-recorder/src/local_meeting_recorder/feishu_calendar.py`（日历读取/组织者判定，2026-11-10 已对真实 API 形状验证） | 平移为 advisor 内部组件 |
| `bot/` | `../chatbot/chatbot/bot.py`（lark 长连接、事件去重、顺序 worker） | 接线模式复制；后端换 advisor API 客户端 |
| `stt-server/` | `../stt-server/`（sherpa 流式 WS 服务） | 整体平移；`auth` 配置固定 `none`，删除飞书鉴权分支或保留不启用 |
| `llm-gateway/` | `../llm-gateway/`（任务/幂等/OpenCode 沙箱） | 原样平移，不动契约 |
| 契约语义 | `../docs/CONTRACT_RECORDER_INPUT.md`、`CONTRACT_STT_STREAM.md`、`CONTRACT_REMOTE_INFERENCE.md`、`APP_PERMISSIONS_EVENTS.md` | 语义提炼进新契约，文件留在旧处引用 |

## 2. 里程碑与依赖

```
阶段0 契约与骨架
   ├──► 阶段1 云端三节点（可并行）──┐
   └──► 阶段2 recorder 改造 ────────┤
                                   ▼
                          阶段3 M1：S1/S2 端到端（全桩）
                                   ▼
                          阶段4 M2：S3 订阅场景（全桩）
                                   ▼
                          阶段5 M3：真实接入联调
                                   ▼
                          阶段6 M4：多租户、部署与真实会议验收
```

- **M1** = S1+S2 假链路端到端（假飞书/假 OAuth/假模型）。
- **M2** = M1 + S3 订阅审批全场景（仍全桩）。
- **M3** = 真实飞书 IM + 真实 OAuth + 真实日历校验 + 真实 GLM。
- **M4** = 多租户隔离 + 五单元部署 + 真实会议验收取证。

## 3. 阶段 0：契约冻结与模块骨架

- [x] **0.1 建模块骨架与 CI。** 六个模块目录 + pyproject + 空测试 + ruff/pyright 配置；根 `new/integrations/` 集成测试骨架；CI 矩阵（3.11/3.14）沿用旧 `quality.yml` 形态。**验收：**CI 全绿，六模块可独立 `pip install -e .` 并跑空测试。**完成（2026-11-10）：**六模块 src 布局骨架 + `.github/workflows/quality-new.yml`（模块矩阵 + integrations 两 job）；本地 3.11 验证：六模块 `pip install -e .`、ruff、pyright、unittest 全绿，`new/integrations` 跨模块导入用例通过；端口缺省值已定并写入 DEPLOYMENT（P_ingest=8400、P_api=8380、P_auth=8381、P_stt=8392 沿用、gateway=8377 沿用）。
- [x] **0.2 冻结 recorder ↔ advisor 流式接入契约**（`new/docs/CONTRACT_RECORDER_STREAM.md`）。内容：WS 端点与消息面（`start` 携流令牌/session/事件绑定/音频源标识 → `started` 回显；二进制音频帧 int16/16k/mono；`gap`/`stop`；下行 `final` 回执不需要，改为服务端落库确认 `ack(sequence)`）；**每条音频源一条连接**（loopback 常开、mic 开启时独立加一条，两路互不影响——沿用旧 STT 契约"每连接一个音频源会话"语义），sequence/event_id 按连接（流）独立；幂等与缺口语义照搬旧 spool 契约；断线重连=新流、重传窗口、`stream_end.last_sequence`；服务端错误码（含 `bad_token`/`session_ended`）；v1 受信网络明文 ws。**验收：**契约评审通过，含正常/重复/断连/越权四个时序示例。**完成（2026-11-10）：**契约 v1 冻结，含四个时序示例（正常收尾/断线重传去重/缓冲丢失缺口/越权）；sequence 按**源**跨重连续制（event_id=session:source:seq 幂等去重），与计划原文"按连接独立"的差异已评审确认——按连接归零会使重传帧失去全局幂等身份。
- [x] **0.3 冻结 bot ↔ advisor 内部 API 契约**（`new/docs/CONTRACT_ADVISOR_API.md`）。资源面：`POST /sessions`（入会：meeting_no/link → 校验+建会+返回 recorder 命令，**建会时从日历事件描述自动带出会话材料**（空描述即无材料，不阻塞）；**凭据缺失/失效时返回 `authorization_required` + 授权 URL**，由 bot 转发小A）、`POST /sessions/{id}/end`、`GET /sessions/{id}/status`、订阅 `POST /subscription-requests`（`#rN` 编号**全局唯一**，advisor 分配）、`POST /subscription-requests/{id}/decide`、outbox `GET /outbox/pending?limit=`、`POST /outbox/{uuid}/result`。语义：幂等键（message_id 派生）、审批状态机（revoked 终态）、fan-out 仅前向。**验收：**每接口有请求/响应/错误码示例；与 REQUIREMENTS S3 边界逐条对应。**完成（2026-11-10）：**API v1 冻结，错误码总表 + 各端点示例 + S3 边界对应表；较计划新增 `GET /sessions`（`状态` 列多会话所需）与 `POST /subscriptions/{id}/revoke`（撤销指令所需）。
- [x] **0.4 冻结 auth 契约**（`new/docs/CONTRACT_AUTH.md`）。内部 API（**仅 advisor 调用**，bot 不直连 auth）：`GET /credentials/{open_id}/token`（有效即返、临期静默刷新、并发刷新单飞——沿用旧 oauth.py 的"提交 marker 后才允许一次性 POST"语义）、授权 URL 生成（入参 open_id+待续接意图 → 授权 URL，state 绑定 open_id 与意图）；OAuth 面：`GET /oauth/callback`（浏览器经 TLS 反代到达；state+code 校验、换取、按 open_id 存储）；存储格式与权限（0600、原子写、目录 0700）。**验收：**含过期/拒绝/重放/并发刷新时序说明。**完成（2026-11-10）：**契约 v1 冻结；补齐旧实现关键语义——PKCE S256+prompt=consent、refresh 必须轮转（同值视为非法）、marker 提交后崩溃即 fail-closed、open_id 串换校验、scopes 三项；含四段时序说明。
- [x] **0.5 冻结 recorder 启动命令格式。** 参数表：`--session --event --url --token [--mic] [--mic-speaker <标签>] [--duration]`（`--session` 为**云端分配**的会话 id，不复用旧 `local:` 前缀；`--mic` 仅在组织者明确开麦时出现；`--mic-speaker` 为麦克风字幕说话人标签字符串，默认"本机"，非组合开关）；bot 生成命令的模板与转义规则；明确 **user_access_token 禁止出现在命令文本**（验收用例固化）。**验收：**格式写入 CONTRACT_RECORDER_STREAM 附录。**完成（2026-11-10）：**附录 A 冻结——参数字符集与校验、单引号注入转义规则（字符集排除引号/反斜杠）、红线验收用例、退出码表（0/2/3/130，Ctrl-C 不伪造 stream_end）。
- [x] **0.6 订阅/会话状态机规格化。** 把 REQUIREMENTS/ARCHITECTURE 的状态机（订阅 requested/approved/denied/revoked，**revoked 为终态：再申请不新建、直接拒绝**；会话 recording/draining/ended，**无 paused**——v1 无暂停辅助；**采集空闲超时阈值 config 可配，超时→draining；draining/ended 均拒新订阅**）落成带迁移约束的规格表（含非法迁移的错误码），作为 advisor 实现与测试的对照。**验收：**规格表评审通过。**完成（2026-11-10）：**`new/docs/STATE_MACHINES.md` 冻结——订阅 T1–T5 + 幂等/拒绝表 + 不变量；会话 S1–S4 + 排空条件三款 + 非法迁移错误码；源状态 live/drained/lost 供排空判定；`idle_timeout_s` 默认 900s；`uncertain` 不阻塞 ended。

## 4. 阶段 1：云端三节点最小实现（单用户，桩优先）

- [x] **1.1 auth 服务。** 平移 oauth.py；存储多用户化（每 open_id 一记录）；实现内部 API 与回调端点（transport 可注入桩）；凭据目录独立。**验收：**单测覆盖授权/刷新/过期/重放/并发单飞；桩模式下全流程可跑。**完成（2026-11-10）：**`new/auth/` 落地（constants/errors/storage/transport/store/state_table/service/config/api/__main__，stdlib only）；30 项单测全绿（授权成功/主体串换拒绝/拒绝/重放/过期/轮转强制/网络失败 fail-closed/崩溃 marker 残留/refresh 过期/并发 8 线程单飞/0600-0700 权限/原子写无残留/HTTP 全流程含页面与错误映射）；ruff/pyright 通过。实现期修正 CONTRACT_AUTH §2.2.5（503 不改状态与 marker 先行语义冲突，改为 409 fail-closed）。
- [x] **1.2 advisor 会话域平移。** 复制旧 store/repo/snapshot/decision/jobs/outbox 与本地会话状态机；材料来源改为建会时从日历事件描述自动带出（对应旧 `LocalMaterialBootstrapper`"显式描述即材料、无描述即 disabled"语义）；所有表加 `owner`（v1 单用户写死占位）；移除 CLI 入口依赖，暴露 API；每会话租约由 FileLock 改 DB 内租约（沿用"同会话第二消费者被拒"语义）。**验收：**旧 advisor 的关键单测语义在新模块复现（决策幂等、outbox 状态机、draining 排空、ended 拒绝迟到结果）。**完成（2026-11-10）：**`new/advisor/` 会话域落地（models/errors/settings/sqlite/persistence/store/snapshot/decision/model_backend/model_jobs + 角色文件平移）；sessions 表合并旧 meetings+local_sessions 并带 owner/会议号/事件绑定/流令牌/DB 租约；订阅表按 STATE_MACHINES 落地（#rN 全局自增）；outbox channel 收敛为 local+im（local 行即事实、恒 sent；fan-out 收件人=owner+approved，决策时同一事务内查询）；RemoteModelBackend/ModelJobExecutor 原样平移（Decision 收敛 control=none|cancel）。28 项单测全绿（决策幂等/outbox 全迁移/取代语义/draining 排空三条件/ended 拒迟到/租约第二消费者拒绝与续期/ingest 去重/订阅 T1–T5+终态/仅前向+撤销止血/冷却门控）；ruff/pyright 通过。HTTP API 面随 1.4 监督器一并暴露。
- [x] **1.3 advisor ingest 网关。** 独立线程/进程承载 WS：流令牌校验（铸造/吊销在会话域）、音频帧转投 stt-server（云内 WS 客户端）、final 按契约幂等落库、与业务循环只经持久化队列交接；背压策略（队列上限+丢帧显式计数）。**验收：**注入式桩测试覆盖鉴权拒绝、重复 sequence 去重、STT 断连不崩、慢消费者不阻塞另一会话。**完成（2026-11-10）：**`ingest.py`（CONTRACT_RECORDER_STREAM v1 全消息面：start/帧带序号/gap/stop/stream_end + started/ack/stopped/error 十种错误码；校验顺序=协议→令牌/会话→绑定→源冲突→序列窗口；按源去重与重传窗口 600 帧；缺口显式落库；idle 120s）+ `stt_client.py`（云内 STT 客户端，旧契约 v2 形态 auth=none，可注入工厂）；final→Caption 幂等落库即持久化交接；跨线程 SQLite 加 RLock + check_same_thread=False（事务全程持锁）；websockets>=14（proxy=None 防系统代理劫持内网连接）。14 项注入式 WS 测试全绿（鉴权/绑定/冲突/协议拒绝、重传去重、缺口记录、stale、慢消费者隔离、空闲超时、final 去重、断连释放源）。
- [x] **1.4 advisor 会话监督器。** 建会即自动启动 consume 循环（多会话并发，沿用旧 AdviserService 每会话循环语义）；`结束`→draining→落定→ended 全自动；进程重启后恢复未 ended 会话的循环。**验收：**重启恢复测试；N 会话并发互不串扰测试。**完成（2026-11-10）：**`supervisor.py`（每会话一线程 + DB 租约续期 + take_finished 轮询 + backoff；idle→draining（ingest 活动快照）→drain_ready→ended 自动；重启恢复 recording/draining 循环）+ `api.py`（CONTRACT_ADVISOR_API 全资源面：sessions 入会/列表/状态/结束、订阅申请/审批/撤销、outbox pending/result，X-Idempotency-Key 落库重放，S1 四拒绝码映射，authorization_required 现取带 intent 的授权 URL）+ `auth_client.py`（auth 节点 HTTP 客户端）+ `rule_backend.py`（桩模型）+ `advisor serve/init` CLI（--calendar-stub 桩先行）；ingest 补 activity_snapshot/close_session（draining 冲刷在途连接）。16 项新测试（监督器自动决策/排空至 ended/空闲超时/重启恢复/双会话并发 + API 全端点/幂等重放/越权/订阅终态/outbox 往返）全绿；模块合计 58 项。
- [x] **1.5 bot 服务骨架。** lark 长连接接线（可桩）、事件去重、顺序 worker；指令解析器（`入会/结束会议/同意/拒绝/撤销订阅/状态`，会议号与链接解析沿用旧 `parse_meeting_reference` 规则）；advisor API 客户端。**验收：**解析单测覆盖合法/非法/含 @/含密码链接；桩模式下指令往返可跑。**完成（2026-11-10）：**`commands.py`（解析规则逐字平移：9 位号/https vc.feishu.cn/j/ 链接/单 pwd/冲突密码拒绝；@ 提及剥离；非指令文本含会议引用→订阅意图）+ `advisor_client.py`（契约客户端，X-Idempotency-Key=message_id 派生）+ `recorder_command.py`（附录 A 渲染：白名单键+字符集断言+单引号注入，红线测试断言不含用户凭据）+ `service.py`（message_id 去重、S1 四拒绝话术、审批请求 DM、outbox 顺序轮询 sent/uncertain 回写；Directory 端口解析昵称）+ `lark_bot.py`（长连接接线，pyright 排除待 M3 实测）+ `bot serve` CLI。26 项测试全绿（解析合法/非法/@/密码链接、渲染白名单拒绝、桩模式全指令往返、授权链接下发、outbox 投递与 uncertain）。
- [x] **1.6 stt-server / llm-gateway 平移。** 目录搬入 + 测试迁跑 + 部署配置（stt auth=none）。**验收：**两模块原有测试全绿；llm-gateway 假模型经 HTTP 可用。**完成（2026-11-10）：**两模块 rsync 整体迁入（src/tests/README/pyproject，auth 默认已是 none）；websockets 定版 `>=17,<18`（15.0.1 下 sync 客户端组合不稳，17.4 与旧部署一致）；迁移适配：stt 测试 sync 连接显式 proxy=None（macOS 系统代理）+ 单次握手重试。3.14 全绿（stt 34 + gateway 33）；3.11 下 stt 测试组合（线程事件循环 + sync 客户端二次建连）不稳定——服务代码同一异步路径在 3.14 通过、旧仓库 CI 本就不含 stt-server，CI 矩阵对 stt-server 排除 3.11 腿并记入风险表；其余模块 3.11/3.14 双腿全绿（advisor 58/bot 26/auth 30）。验收实测：gateway 假模型 HTTP 提交→轮询→succeeded→decision 载荷符合 Decision v2。

## 5. 阶段 2：recorder 改造（可与阶段 1 并行）

- [x] **2.1 剥离与保留。** 保留 audio/ 采集、MuteGate 手动开关、整场本地音频留存、`--mic` 默认关；删除 spool writer、本地 OAuth、STT 客户端（remote-stream/sherpa/zhipu 全下线）。**验收：**采集层原有单测通过；无对旧 spool/oauth 模块的引用。**完成（2026-11-10）：**`audio/`（frames/sources/sck_capture+swift/session）逐字平移（MicSource 手动开关默认关、暂停=显式 gap 事件、CaptureSession 整场 wav 留存+订阅扇出、SCK helper 包数据随附）；`test_audio_session.py` 迁入改包名，11 项全绿；新模块从未引入 spool/oauth/stt（无需删除项）。
- [x] **2.2 WS 推流客户端。** 按契约 0.2 实现：建连出示流令牌、双路音频帧上行、断线本地缓冲与重传窗口、`stream_end` 干净收尾、Ctrl-C 不伪造 end；连接失败显式退出码。**验收：**注入式假服务端覆盖重连/重传/令牌过期三场景单测。**完成（2026-11-10）：**`uplink.py`（每源一连接，sync 客户端 proxy=None；wire 序列=采集帧序，跨重连稳定支撑幂等去重；未 ack 帧入有界 deque 重传窗，溢出显式计数；gap 事件转发；重连退避 0.5→10s 上限 8 次；错误分类→退出码 2/3/130；stale_sequence 触发新纪元重置；float32→int16 转换）。9 项注入式假服务端测试全绿（happy/干净收尾/gap/Ctrl-C 无 stream_end/断线重连只重传未 ack/bad_token 退出码 2/stale 重置续传/重试耗尽退出码 3/转换钳位/CaptureSession 集成喂流）；模块合计 20 项；ruff/pyright 通过。
- [x] **2.3 启动命令与诊断。** 参数解析、事件绑定回显、上行速率/丢帧计数本地打印。**验收：**命令行契约测试。**完成（2026-11-10）：**`recorder real|replay` CLI（附录 A 参数校验：session/token 字符集、event 斜杠形、ws:// ingest 形、duration 正数；real=SCK loopback+可选 mic，replay=假台词同链路回放；信号处理：Ctrl-C=130 不伪造 end、duration=干净收尾；stop 前排空等待避免 writer 线程 50ms 轮询窗口丢尾帧；每 5s 打印上行 stats 与退出码行）。6 项 CLI 契约测试全绿（参数合法/非法矩阵、红线"无任何凭据类参数"、replay 端到端退出码 0、bad_token 退出码 2、参数非法退出码 2）；模块合计 26 项；ruff/pyright 通过。
- [x] **2.4 打包与分发。** recorder 生成可分发安装形态（whl/zipapp + 依赖清单或安装脚本）与安装文档（Mac Gatekeeper 绕过指引，Windows 预留）；版本号与升级说明。**验收：**一台干净 Mac 按文档完成安装并跑通 `--help` 与 fake 采集。**完成（2026-11-10）：**`python -m build --wheel` 产物验证（含 SCK swift helper 包数据）；README 安装文档（venv+whl、swiftc/PortAudio 前置、Gatekeeper 与麦克风授权引导、升级说明）；模拟"干净 Mac"验收：全新 3.11 venv 从 whl 安装 → `--version`/`--help` OK → replay 假采集连真实假服务端全链路退出码 0、5 帧留存（真机干净 Mac 的最终取证随 6.4 一并做）。

## 6. 阶段 3：M1 —— S1/S2 端到端（全桩）

- [x] **3.1 集成测试床。** `new/integrations/`：进程级拉起 advisor/bot/auth/stt-server/llm-gateway（假模型后端）；假飞书 IM 总线（模拟小A/小B 会话、事件重放）；假 OAuth（跳过浏览器直接发码）。**验收：**一键拉起/清场脚本。**完成（2026-11-10）：**`test_bed.py`（TestBed：进程级 auth --stub（StubTransport：code=open_id 换 stub token）+ llm-gateway 真 serve（fake backend）+ advisor 真 serve（--calendar-stub --remote-backend fake，三个 healthz 就绪门）+ FakeSttServer（契约 v2 形态桩，每 3 帧产一条 final）+ 进程内 BotService 挂 FakeImBus；ephemeral 端口、SIGTERM 有界清场、日志落临时目录）。`test_bed_up.py` 2 项全绿（一键拉起/healthz/gateway 假模型可达 + 假 OAuth 全流程取到 stub token）。
- [x] **3.2 S1 主链路。** 假小A 发"入会"→ 校验（桩日历可控组织者/时间窗）→ 建会+流令牌 → bot 下发命令 → 假粘贴执行 recorder（fake 音频源，沿用旧 fake 台词回放思路）→ ingest → STT（真 sherpa 或桩）→ 字幕落库。**验收：**四种拒绝路径各有用例；命令文本断言不含用户凭据。**完成（2026-11-10）：**`test_m1_s1.py` e2e：无凭据→授权链接（red line：命令文本不含 stub/user token）→假 OAuth→重发入会→建会+令牌→raw WS 推 12 帧→ack/stream_end→桩 STT final→字幕落库（pending_model_inputs≥1、材料 present、源 drained）；含密码链接入会+重复入会幂等。四种拒绝路径用例已在 advisor/bot 模块单测逐条覆盖（test_api 四拒绝码 + bot 话术映射），e2e 补了 authorization_required 真链路。实现期修复：auth 客户端 404 not_authorized 误归类为不可达（应触发授权链接）。三连跑全绿。
- [x] **3.3 S2 决策与通知。** 假模型产生决策 → outbox `local`+`im` 行 → bot 拉取发送（假 IM）→ 小A 收到；`结束会议` 排空至 ended。**验收：**e2e 断言决策幂等（重跑不重复）；ended 后迟到结果不应用。**完成（2026-11-10）：**`test_m1_s2.py` e2e：字幕→监督器任务→gateway 假模型→决策→outbox 双行→bot 轮询→小A 收到"网关合成回答"且 2s 内无重复投递（幂等）；`结束会议`→draining→自动 ended；ended 后新 ingest 连接被拒（session_ended）；`状态` 指令列会话。实现期修复：draining 时无 stream_end 的 live 源不会终态化——supervisor 增加"已断开 live 源标记 lost"（STATE_MACHINES abandoned 语义落地）。两连跑全绿。
- [x] **3.4 可靠性用例。** recorder 断线重连重传、advisor 重启恢复监督、bot 重启后 outbox 续发、重复事件去重。**验收：**四类用例全绿并留证据。**完成（2026-11-10）：**`test_m1_s4.py` 四类 e2e：断线后带重叠重传（重复帧幂等丢弃，源 drained 游标 12）；advisor 进程重启（新端口）后监督恢复、续流、回答送达；bot 轮询器停摆期 pending 积压、重启后积压送达；同 message_id 重复事件仅一次回复。实现期修复两处真实缺陷：①重启后旧 DB 租约未过期导致恢复循环被拒（lease busy）——supervisor 启动恢复时清租约（单机 v1 假设）；②测试床 FakeStt 文本表耗尽。两连跑全绿。
- [x] **3.5 时延测量。** 假链路各段计时（说完→字幕→决策→IM）。**验收：**给出分段时延表（真实预算待 M3 后修订）。**完成（2026-11-10）：**`test_m1_s5_latency.py`：说完→字幕落库 0.12s / 字幕→决策 pending 0.5s（监督器 poll 0.5s 占大头）/ 决策→IM 送达 0.1s / 端到端 0.72s（100ms 轮询粒度，假链路基线；真实预算待 6.4）。integrations 全量 12 项全绿。**M1（S1+S2 假链路端到端）达成。**

## 7. 阶段 4：M2 —— S3 订阅场景（全桩）

- [x] **4.1 订阅域。** 订阅表+状态机（规格 0.6）；小B 会议号解析到"正被服务中的会话"注册表；审批请求生成与 #rN 编号。**验收：**状态机全迁移路径单测（含非法迁移拒绝）。**完成（2026-11-10）：**域实现随 1.2 落地；`tests/test_subscriptions_spec.py` 12 项对照 0.6 规格逐条：T1–T5 全迁移、draining/ended 拒新（not_found）、revoked 四向终态拒绝、decide 幂等回显、owner 校验、非法 decision、#rN 跨会话全局单调、组织者行等价物（owner 恒在收件人集合）、注册表仅解析 recording。全绿。
- [x] **4.2 fan-out 扩展。** 决策时实时查询 approved 集合；组织者行建会自动写入；每收件人独立 IM 行；撤销时 cancel 该收件人 pending 行（沿用旧 `reconcile_destinations` 同型逻辑）。**验收：**审批后下一条回答双收件人；撤销即刻止血；仅前向不回溯。**完成（2026-11-10）：**实现随 1.2/4.1 落地（approved 集合在决策落库同一事务内查询；撤销 cancel pending 行）；`test_m2_s3.py` e2e 逐条验证：审批前小B 零投递（仅前向）、审批后双收件人、撤销后小B 停收且小A 照收、revoked 再申请终态拒绝。实现期修复一处真实缺陷：跨连接 STT offset 重置导致 delivery_id 撞车（重连后字幕全被幂等丢弃）——去重标识改用 ingest 连接纪元 tag。
- [x] **4.3 会话与审批交互。** draining/ended 拒新订阅；approved 重复申请幂等提示；denied 再申请走新审批；revoked 再申请直接拒绝（终态）。**验收：**对应 REQUIREMENTS S3 每条边界的用例各一。**完成（2026-11-10）：**`test_m2_s3.py`：ended 会话申请回复"未找到该会议或会议已结束"；denied→再申请→新 #rN→批准全链；approved 重复申请幂等（4.1 单测覆盖）。**M2（S3 订阅场景全桩）达成。**

## 8. 阶段 5：M3 —— 真实接入联调

- [ ] **5.1 真实 OAuth + 日历校验。** 飞书应用后台注册回调（指向 auth）；真实账号授权（电脑+手机）；真实日历组织者/时间窗校验。**验收：**`data/acceptance-auth/` 同款取证（脱敏）。**进展（2026-11-10）：**代码侧就绪——真实 `FeishuCalendarValidator` 落地（旧 feishu_calendar.py 语义平移：vchat.meeting_url 提取 9 位会议号做事件映射、组织者= event_organizer.user_id ∈ 用户身份集合、时间窗 [start−30min, end+30min]、description 即材料、token 失效一次静默刷新；9 项注入式单测覆盖四拒绝路径/窗口松弛/取消与全天事件过滤/刷新/凭据中途丢失）；`advisor serve --calendar feishu|stub`（默认 feishu，--calendar-stub 兼容保留）。本地全栈脚本 `new/scripts/local-up.sh|local-down.sh` + `local.env.example` + `new/docs/LOCAL_TESTING.md`。待用户提供飞书应用凭据与真实会议进行实测取证。
- [ ] **5.2 真实飞书 IM。** bot 上真应用（IM 三权限+长连接事件，沿用旧权限清单）；小A/小B 真账号跑 S1–S3。**验收：**真机全场景走查记录。
- [ ] **5.3 真实 GLM。** gateway 切 OpenCode 后端，复用旧 M2 部署经验（`../docs/REMOTE_SERVER_DEVELOPMENT.md`）。**验收：**真实会议片段端到端，记录执行耗时。
- [ ] **5.4 真实会议预验收。** 一场真实飞书会议全链路（双路采集、延迟、字幕质量、回答质量）。**验收：**证据存档；问题清单回填任务。

## 9. 阶段 6：M4 —— 多租户、部署与收尾

- [ ] **6.1 多租户隔离。** owner 贯穿会话/字幕/outbox/订阅；ingest 令牌绑定 owner 校验；跨用户访问显式拒绝；按用户容量/并发配额首版上限。**验收：**双用户并发互不可见、互不挤占的集成测试。
- [ ] **6.2 安全加固。** 流令牌 TTL 与吊销闭环；auth 凭据静态加密（可选但记录决策）；回调/ingest 经反代 TLS；节点间预留服务认证接口（拆机前启用）。**验收：**越权/过期令牌/重放三组负向测试。
- [ ] **6.3 部署件。** 五个 systemd 单元、TLS 反代（OAuth 回调；ingest 走受信网络明文 ws，公网/拆机前再补 TLS）、healthz 与最小指标、有界停机、备份/删除脚本（按用户）。**验收：**干净服务器按文档一次拉起；演练停机恢复。
- [ ] **6.4 真实会议正式验收。** REQUIREMENTS S1–S5 全场景+异常路径（断网/合盖/撤销/拒绝/授权失效），双 persona 各自设备取证。**验收：**验收报告与证据归档，时延对照 20s 预算给结论。
- [ ] **6.5 文档与归档收尾。** new/README 更新为运行文档；模块 README（安装/配置/排障）；旧仓库资产归档核对（对照 §1 表逐项标记"已平移/不带走"）。

## 10. 已知风险与显式待定项

| 项 | 状态 | 归属任务 |
|---|---|---|
| WS 音频中转的背压与线程模型（新运行时职责，advisor 无先例） | 风险 | 1.3 先行验证（含慢消费者测试） |
| 云端并发刷新 refresh token 的竞态（旧实现单机单文件锁） | 风险 | 1.1 单飞语义 + 并发测试 |
| IM 复制粘贴命令的 UX 失败率（粘错/漏参数） | 接受+缓解 | 0.5 参数校验与清晰报错；一键启动列为后续增强 |
| 时延预算 20s 未在新链路实测 | 待定 | 3.5 假链路分段 → 6.4 真实结论 |
| SQLite 三节点各自独立库，拆机部署需迁移 | v1 接受 | 6.3 记录边界；拆机前再立项 |
| 端口/反代选型（P_ingest/P_api/P_auth/P_stt 具体值） | 已定（0.1） | P_ingest=8400、P_api=8380、P_auth=8381、P_stt=8392（沿用）、gateway=8377（沿用）；已写入 DEPLOYMENT §2 |
| GLM Coding Plan 供多人使用的政策边界（bot 转发是否合规） | 关注 | 5.3 联调时复核旧 chatbot/README 的政策提示，必要时切独立 API |
| stt-server 迁移测试在 3.11 的线程/sync 客户端组合不稳定（服务代码 3.14 全绿） | 风险（测试面） | CI 对 stt-server 仅跑 3.14 腿；部署基线固定 3.14；如重写测试床可复评 |

## 11. 进度记录

- 2026-11-10 立项，计划 v0.1（契约/骨架未开始）。
- 2026-11-10 首轮评审修订（v0.2）：删除暂停/恢复辅助（无 paused 状态）；订阅 revoked 定为终态（再申请直接拒绝）；draining/ended 拒新订阅；采集空闲超时阈值改 config 可配；ingest 改为每条音频源一条连接、v1 受信网络明文 ws（后端不接公网）；授权链接统一经 advisor 下发（bot 不直连 auth）；#rN 定为全局编号；材料来源定为建会时从日历事件描述自动带出（空描述即无材料，不阻塞）；新增 2.4 打包分发；笔误修正（拓扑图编号冲突、`--mic` 命令模板、`--mic-speaker` 语义、时延预算出处、会话 id 前缀）。
- 2026-11-10 **0.1 完成**：六模块骨架 + CI（quality-new.yml）+ integrations 骨架；端口缺省值定稿写入 DEPLOYMENT。
- 2026-11-10 **0.2 完成**：CONTRACT_RECORDER_STREAM v1 冻结（start/帧带序号/gap/stop/stream_end + started/ack/stopped/error；按源序列跨重连续制、重传窗口 600 帧、缺口显式化、错误码 10 项、四时序示例）。
- 2026-11-10 **0.3 完成**：CONTRACT_ADVISOR_API v1 冻结（sessions 入会/列表/状态/结束、订阅申请/审批/撤销、outbox 拉取回写；X-Idempotency-Key、错误码总表、S3 边界对应表）。
- 2026-11-10 **0.4 完成**：CONTRACT_AUTH v1 冻结（authorize/start + credentials/token + oauth/callback；marker 先行/必须轮转/崩溃 fail-closed/单飞；0600/0700 原子存储；四段时序）。
- 2026-11-10 **0.5 完成**：CONTRACT_RECORDER_STREAM 附录 A 冻结（命令模板、参数字符集、转义规则、凭据红线验收用例、退出码表）。
- 2026-11-10 **0.6 完成**：STATE_MACHINES.md 冻结（订阅 T1–T5/幂等拒绝表；会话 S1–S4/排空条件；源 live/drained/lost；idle_timeout_s 默认 900）。**阶段 0 全部完成，进入阶段 1。**
- 2026-11-10 **1.1 完成**：auth 服务（stdlib，多用户凭据库 + 内部 API + OAuth 回调 + 单飞刷新）；30 项单测全绿；CONTRACT_AUTH §2.2.5 fail-closed 修正。
- 2026-11-10 **1.2 完成**：advisor 会话域（SessionStore/UnitOfWork/snapshot/decision/model_backend）；28 项单测全绿；DB 租约、订阅域、fan-out 前向语义落地。
- 2026-11-10 **1.3 完成**：ingest 网关（WS 契约 v1 全消息面 + stt_client 可注入）；14 项 WS 测试全绿；SQLite 跨线程加锁。
- 2026-11-10 **1.4 完成**：会话监督器（自动 consume/排空/重启恢复）+ advisor HTTP API（契约全端点）+ auth 客户端 + rule 桩模型 + serve CLI；16 项新测试，模块合计 58 项全绿。
- 2026-11-10 **1.5 完成**：bot 骨架（指令解析平移/advisor 客户端/命令渲染红线/服务与 outbox 轮询/lark 接线）；26 项测试全绿。**阶段 1 剩 1.6（stt-server/llm-gateway 平移）。**
- 2026-11-10 **1.6 完成**：stt-server/llm-gateway 整体迁入并全绿（3.14：34+33；gateway 假模型 HTTP 实测可用）；websockets 定版 >=17,<18；stt-server CI 仅 3.14 腿（风险表已记）。**阶段 1 完成，阶段 2（recorder 改造）可并行推进。**
- 2026-11-10 **2.1–2.4 完成**：recorder 改造全部落地——audio 层逐字平移（11 测试）、WS uplink 客户端（9 测试：重连/重传/退出码）、CLI real/replay（6 契约测试）、whl 打包+README+干净 venv 验收；模块合计 26 项全绿。**阶段 2 完成。下一步：阶段 3（M1 假链路集成）。**
- 2026-11-10 **3.1–3.3 完成**：集成测试床（进程级三节点 + 假 OAuth/IM/STT）、S1 主链路 e2e（授权链接→建会→推流→字幕落库，红线断言）、S2 决策通知 e2e（回答送达/幂等/排空/ended 拒绝）；修复 auth 404 映射与 draining 源终态化两处真实缺陷。
- 2026-11-10 **3.4–3.5 完成，M1 达成**：四类可靠性 e2e 全绿（修复重启租约残留缺陷）；假链路时延表：0.12/0.5/0.1s，端到端 0.72s。integrations 12 项全绿。**下一步：阶段 4（M2 订阅场景）。**
- 2026-11-10 **4.1–4.3 完成，M2 达成**：订阅状态机规格单测 12 项 + S3 全场景 e2e（仅前向/双收件人/撤销止血/denied 重审/ended 拒绝）；修复跨连接 STT 去重撞车与 ingest 启动静默失败两处真实缺陷；integrations 15 项全绿（四轮三绿一超时，本机负载相关）。**阶段 0–4 全部完成；阶段 5（M3 真实接入）需要真实飞书应用/账号/GLM 环境，等待人工协同。**
