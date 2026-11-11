# Recorder 流式接入契约（recorder ↔ advisor ingest）

状态：v1 于 2026-11-10 冻结（阶段 0.2）。客户端为 `new/recorder`（WS 推流），服务端为 `new/advisor` 的 ingest 网关。变更需 bump `protocol` 版本并同步两侧实现与测试。上游设计：[ARCHITECTURE](ARCHITECTURE.md) D2/D3；语义来源：旧 `docs/CONTRACT_RECORDER_INPUT.md`（幂等/确认）与旧 `docs/CONTRACT_STT_STREAM.md`（WS 形态/断连）。

## 1. 传输与基线

- WebSocket（RFC 6455），端点 `ws://<host>:8400/ingest`。v1 受信网络明文 ws（公网/拆机前补 TLS，见 [DEPLOYMENT](DEPLOYMENT.md) §3）。
- **每条连接承载一个音频源**：loopback 一条、mic（开启时）一条，同会话两源两连接，序列空间与识别会话完全独立、互不影响（沿用旧 STT 契约语义）。
- 文本帧为 UTF-8 JSON 对象，必含 `type`；二进制帧为带序号的音频（第 2.2 节）。
- 音频：**int16 LE、单声道、16 kHz**；建议帧长 100 ms（1600 采样）；单帧上限 1 MiB。
- 建连鉴权用**流令牌**（advisor 建会时铸造、随启动命令下发、绑定单一 session、短期、会话结束即吊销）。**任何用户 OAuth 凭据不得出现在本协议任何消息中。**
- 服务端默认限制：空闲超时 120 s（任何消息重置，超时冲刷后 `idle_timeout` 关闭）；心跳 ping/pong 20 s。

## 2. 客户端 → 服务端

### 2.1 `start`（文本，必须为第一条消息）

```json
{"type": "start", "protocol": 1, "session_id": "s-3f9c", "stream_token": "st-xxx",
 "source": "loopback", "event": "cal_xxx/evt_yyy",
 "resume": false, "first_sequence": 1, "wall_clock_at": "2026-11-10T14:00:00+08:00"}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `protocol` | int | 是 | 当前为 `1`，不匹配拒绝（`bad_protocol`） |
| `session_id` | str | 是 | advisor 建会分配的会话标识（云端命名空间，无 `local:` 前缀） |
| `stream_token` | str | 是 | 流令牌；校验会话存在、令牌匹配、未过期未吊销 |
| `source` | str | 是 | `loopback` \| `mic`；mic 连接须额外携带 `speaker`（字幕说话人标签，默认"本机"） |
| `event` | str | 是 | 绑定的日历事件 `<calendar_id>/<event_id>`，与会话建会时的绑定一致，否则 `bad_binding` |
| `resume` | bool | 是 | `false` = 采集起始；`true` = 断线重连续传（第 4 节） |
| `first_sequence` | int | 是 | 本连接将发送的第一帧的 `sequence` |
| `wall_clock_at` | str | 是 | `first_sequence` 帧的采集墙钟（RFC3339），作为该源时间锚点 |

### 2.2 音频（二进制帧）

- 帧格式：**4 字节 LE uint32 `sequence` + int16 LE PCM 数据**。
- `sequence` 由 recorder 在**采集时**按源单调分配（从 1 起，uint32），**跨重连连续**（重连不归零）。`event_id = "<session_id>:<source>:<sequence>"`，全局唯一，服务端据此去重。
- 静音请继续发送零值帧（保持时间轴与端点检测连续）。
- 单连接内 `sequence` 必须严格递增；回退到已确认序号之外的值属 `bad_message`。

### 2.3 `gap`（文本）

```json
{"type": "gap"}
```

显式断句/暂停边界（MuteGate 门控切换、静音检测边界）：作用于该消息之前已发送的音频；服务端冲刷当前未完结语句（产生 final），后续音频从新语句开始。

### 2.4 `stop`（文本，v1 预留）

```json
{"type": "stop"}
```

临时暂停上行：冲刷当前语句，连接保持，后续可继续发帧。v1 recorder 不使用，为长时静音/挂起场景预留。

### 2.5 `stream_end`（文本，干净收尾）

```json
{"type": "stream_end", "last_sequence": 5183}
```

- `last_sequence` 为本源最后一帧的 `sequence`。服务端持久化补齐到该序号、将源标记 **drained**、回 `stopped` 后关闭连接。
- 仅在干净停止时发送（`--duration` 到时、正常退出流程）；**Ctrl-C 中断不发送**（沿用旧"中断不伪造 stream_end"语义），此时源由服务端空闲超时/会话排空兜底。

## 3. 服务端 → 客户端

### 3.1 `started`

```json
{"type": "started", "stream_id": "s-3f9c:loopback:9f21", "acked_sequence": 0}
```

- `stream_id = <session_id>:<source>:<uuid4>`，**每连接新建**（重连 = 新流）。
- `acked_sequence` 为该源当前持久化游标（首连为 0；重连续传时为服务端已持久化前缀，客户端据此裁剪重传缓冲）。

### 3.2 `ack`

```json
{"type": "ack", "sequence": 410}
```

- `sequence` = 已**持久化**的连续最大前缀（先落库后确认，沿用旧 ack 语义）。recorder 可安全丢弃 `≤ sequence` 的缓冲。
- 发送节奏：每 ≤1 s 或每 10 帧至少一次；`stream_end` 处理时必发。
- `ack` 只承诺持久化，不承诺已转写；转写进度与服务端内部幂等（已转写游标）不在本契约面。

### 3.3 `stopped`

```json
{"type": "stopped", "acked_sequence": 5183}
```

仅在响应 `stream_end` 时发送（此时 `acked_sequence == last_sequence`），随后关闭连接。

### 3.4 `error`（随后关闭连接）

```json
{"type": "error", "code": "bad_token", "message": "…"}
```

| code | 语义 | 客户端行为 |
|---|---|---|
| `bad_protocol` | protocol 版本不支持 | 停止重试，报告用户 |
| `bad_message` | 非法消息/帧（首帧非 start、二进制长度非法、sequence 回退、未知 type） | 停止重试，报告用户 |
| `bad_token` | 流令牌未知/过期/吊销/与会话不匹配 | 停止重试（会话可能已被结束），提示用户找 bot 重新入会 |
| `session_ended` | 会话已 ended | 同上 |
| `bad_binding` | `event` 与会话绑定不一致 | 停止重试，检查命令 |
| `source_conflict` | 同 `(session, source)` 已有活跃连接 | 等待旧连接释放后重试 |
| `stale_sequence` | 重传起点早于服务端保留窗口（第 4.3 节） | 丢弃旧缓冲，从当前采集点重连（`resume:false`） |
| `overloaded` | 超出服务端并发/配额 | 退避重试 |
| `internal` | 服务端内部错误 | 退避重试 |
| `idle_timeout` | 空闲超时（冲刷尾部语句后关闭） | 若会话未结束则重连续传 |

## 4. 断线、重传与幂等

### 4.1 重连 = 新流、旧句丢失

- 连接中断 = 该连接的流终止；服务端侧识别会话随之结束，**断点处未完结语句丢失**（与旧 STT 契约一致，不伪造连续语句）。
- 重连即新 `stream_id` + 新识别会话；**音频 `sequence` 按源连续不归零**，重传的音频进入新识别会话重新转写。

### 4.2 重传窗口与去重

- recorder 缓冲所有未 `ack` 的帧（内存/本地临时文件），重连 `start{resume:true, first_sequence: F}` 从 `F = 已知 ack + 1`（不早于）开始重发。
- 服务端对 `sequence ≤ 持久化游标` 的帧按 `event_id` **幂等丢弃**；对窗口内已接收的重复帧同样丢弃。
- **保留窗口** `W` 默认 600 帧（100 ms 帧 ≈ 60 s）：`F` 晚于 `游标 + 1` 属正常重传；服务端可容忍的重叠范围以 `W` 为界。

### 4.3 缺口显式化

- `F > 游标 + 1`（recorder 缓冲丢失，如进程重启）：服务端将 `(游标, F)` 记为**显式丢失区间**，冲刷当前语句、内部产生 gap 语义，从 `F` 继续；不等待、不跳读补齐。
- `F < 渳标 + 1 − W`（重传过旧）：返回 `stale_sequence`；客户端丢弃旧缓冲，以 `resume:false` 从当前采集点新建源序列（旧序列号作废，服务端按新锚点接续）。
- 确认游标只推进**连续前缀**（沿用旧 spool 语义）。

### 4.4 会话侧兜底

- 会话 draining/ended 后：新连接返回 `session_ended`；在途连接由服务端主动关闭（冲刷后）。
- 采集空闲超时（advisor 配置项）触发会话 draining 的逻辑在会话域，不在本契约面；ingest 连接自身只有 120 s 空闲上限。

## 5. 服务端实现要求（advisor ingest）

- 校验顺序：`protocol` → `stream_token`/会话状态 → `event` 绑定 → `source` 冲突 → 序列窗口；全部通过才创建流并回 `started`。
- 每连接独立读写循环；单源慢消费者不得阻塞其他源/会话（背压与队列上限为 advisor 内部策略，显式计数）。
- `ack` 必须在**持久化事务提交后**发送；advisor 崩溃重启后从持久化游标继续，不依赖内存状态。
- 帧进入识别前先落输入日志；已转写游标独立持久化，重启后不重复转写。

## 6. 时序示例

### 6.1 正常收尾

```text
C> {"type":"start","protocol":1,"session_id":"s-3f9c","stream_token":"st-xxx",
    "source":"loopback","event":"cal_x/evt_y","resume":false,"first_sequence":1,
    "wall_clock_at":"2026-11-10T14:00:00+08:00"}
S< {"type":"started","stream_id":"s-3f9c:loopback:9f21","acked_sequence":0}
C> <binary: seq=1, 3200B> ×N
S< {"type":"ack","sequence":10}
C> …
S< {"type":"ack","sequence":5183}
C> {"type":"stream_end","last_sequence":5183}
S< {"type":"stopped","acked_sequence":5183}
（连接关闭；源 drained）
```

### 6.2 断线重连 + 重传去重（重复）

```text
… C> <binary: seq=50>（连接中断，服务端游标=40，帧 41–50 已收到未确认/或部分丢失）
C> {"type":"start", …, "source":"loopback","resume":true,"first_sequence":41,
    "wall_clock_at":"2026-11-10T14:12:30+08:00"}
S< {"type":"started","stream_id":"s-3f9c:loopback:aa07","acked_sequence":40}
C> <binary: seq=41> … <binary: seq=50>      （重传；已持久化部分被幂等丢弃）
C> <binary: seq=51> …                        （缓冲的断线期间音频继续上行）
S< {"type":"ack","sequence":180}
```

断点处未完结语句丢失；重传音频在新识别会话中转写。

### 6.3 缓冲丢失（缺口显式化）

```text
（recorder 进程重启，本地缓冲丢失；服务端游标=40）
C> {"type":"start", …, "resume":true,"first_sequence":200, …}
S< {"type":"started","stream_id":"s-3f9c:loopback:bb33","acked_sequence":40}
   （服务端记录丢失区间 41–199，冲刷，从 200 继续）
C> <binary: seq=200> …
```

### 6.4 越权/状态错误

```text
C> {"type":"start","protocol":1,"session_id":"s-3f9c","stream_token":"st-WRONG",
    "source":"loopback","event":"cal_x/evt_y","resume":false,"first_sequence":1,
    "wall_clock_at":"…"}
S< {"type":"error","code":"bad_token","message":"unknown or expired stream token"}
（连接关闭）

C> {"type":"start", …, "session_id":"s-3f9c","stream_token":"st-xxx", …}
S< {"type":"error","code":"session_ended","message":"session already ended"}
（连接关闭）
```

## 7. 版本

`protocol=1`。不兼容变更递增版本号，并同时更新 recorder / advisor 两侧实现、测试与 `new/integrations/` 用例后方可切换。

## 附录 A · recorder 启动命令格式（阶段 0.5 冻结）

bot 下发给小A 的命令由 advisor 响应中的 `recorder_command_args`（CONTRACT_ADVISOR_API §2）按以下模板渲染；小A 复制整块到终端粘贴执行。

### A.1 模板

```sh
recorder real --session <session_id> --event '<calendar_id>/<event_id>' \
  --url ws://<host>:8400/ingest --token <stream_token> [--mic] [--mic-speaker <标签>] [--duration <秒>]
```

`--mic` 仅在组织者明确选择开启麦克风时由 bot 追加；`--duration` 可选（按时长干净停止，写 `stream_end`）。

### A.2 参数表与校验规则

| 参数 | 必填 | 格式/字符集 | 说明 |
|---|---|---|---|
| `--session` | 是 | `[A-Za-z0-9-]{6,64}`，云端分配 | 会话 id（advisor 命名空间，**无** `local:` 前缀） |
| `--event` | 是 | `<cal_id>/<event_id>`，各段 `[A-Za-z0-9_.@~-]{1,256}`，至少一段斜杠分隔（2026-11-11 本地实测修正：真实日历 ID 可含 `.@~`，事件 ID 可自带 `/`，段间以 `/` 分隔逐段校验） | 建会时绑定的日历事件，与 ingest 校验一致（`bad_binding`） |
| `--url` | 是 | `ws://<host>:8400/ingest` | ingest 端点；v1 明文 ws |
| `--token` | 是 | `[A-Za-z0-9_-]{16,128}` | 流令牌，仅绑定本会话，短期 |
| `--mic` | 否 | 开关 | 显式开启本机麦克风；默认不采集（隐私优先） |
| `--mic-speaker` | 否 | 字符串，默认 `本机` | 麦克风字幕的说话人标签（非组合开关） |
| `--duration` | 否 | 正整数（秒） | 到时干净停止（发 `stream_end`）；缺省运行至 Ctrl-C |

recorder 启动时逐项校验；任一不合法 → 打印原因并以退出码 2 退出，不建连。

### A.3 bot 渲染与转义规则

- 各参数值先按 A.2 字符集断言，再以**单引号包裹**注入模板；字符集已排除单引号/反斜杠/空白，无需嵌套转义。断言失败 = 渲染 bug，拒绝下发。
- 反斜杠续行仅为可读性；粘贴执行等价单行。
- 命令面向 POSIX sh（macOS 先行）；Windows 分发形式后续增强。

### A.4 红线与验收用例（固化）

- **user_access_token 及任何用户 OAuth 凭据禁止出现在命令文本**：bot 渲染白名单仅 A.2 参数（来源为 advisor `recorder_command_args`），实现不得引入其他来源。
- 验收用例（进 `new/integrations/`）：断言生成的命令文本仅由白名单参数构成，且不包含 advisor 侧任何已知 token 值。

### A.5 退出码（与 §2.5/阶段 2 任务 2.2 对应）

| 码 | 含义 |
|---|---|
| 0 | 干净停止（`stream_end` 已确认） |
| 2 | 参数/鉴权非法（`bad_token`/`bad_binding`/参数校验失败） |
| 3 | 连接不可恢复（`session_ended`/`stale_sequence` 后未续/重试耗尽） |
| 130 | Ctrl-C 中断（**不伪造** `stream_end`，缓冲保留待重连） |

