# 流式 STT 服务契约（stt-server v1）

状态：2026-11-10 冻结（阶段 5.11）。服务端为独立模块 `stt-server/`（sherpa-onnx 流式模型）；客户端为 `local-meeting-recorder` 的 `stt: remote-stream` 模式。变更需 bump 协议版本并同步两侧模块与测试。

## 1. 传输与基线

- WebSocket（RFC 6455），端点 `ws://<host>:<port>/`，每条连接承载**一个音频源会话**（如 mic / loopback 各一条连接）。
- 文本帧为 UTF-8 JSON 对象；二进制帧为音频。
- 音频二进制帧：**int16 LE、单声道、16 kHz**（每个采样 2 字节，长度须为 2 的倍数）。服务端内部转 float32 识别。
- 首版内部部署**无认证**（与 llm-gateway 基线一致）；接公网前需补认证（根 TODO 统一跟踪）。
- 服务端默认限制：并发会话数、每识别器线程数、单帧最大 1 MiB、空闲超时 60 s（任何消息重置）、心跳 ping/pong 20 s。

## 2. 客户端 → 服务端

### 2.1 `start`（文本，必须为第一条消息）

```json
{"type": "start", "protocol": 1, "source_id": "mic", "rate": 16000, "channels": 1}
```

- `protocol`：当前为 `1`，不匹配则拒绝。
- `rate`/`channels`：v1 仅支持 16000/1，其他值返回 `unsupported_format`。
- `source_id`：仅供诊断回显，不参与语义。

### 2.2 音频（二进制帧）

- 连续发送；服务端按收到的采样数累计**源时间轴偏移**（与客户端喂入的采样位置一一对应）。
- 静音请继续发送零值帧（保持时间轴与端点检测连续）。

### 2.3 `gap`（文本）

```json
{"type": "gap"}
```

显式断句/暂停边界：服务端立即冲刷当前未完结语句（产生 `final`），后续音频从新语句开始。等价于本地会话的 `feed_gap()`。

### 2.4 `stop`（文本）

```json
{"type": "stop"}
```

干净结束：冲刷尾部语句，回 `stopped` 后关闭连接。发送后不得再发任何消息。

## 3. 服务端 → 客户端

### 3.1 `started`

```json
{"type": "started", "protocol": 1, "model": "sherpa-...-int8-2026-06-05"}
```

### 3.2 `partial`（可修订，不落稳定上下文）

```json
{"type": "partial", "text": "…", "start_offset": 0, "end_offset": 32000}
```

### 3.3 `final`（语句完结）

```json
{"type": "final", "text": "…", "start_offset": 0, "end_offset": 48000}
```

- `start_offset`/`end_offset`：源时间轴采样偏移（16 kHz）。`final` 之间首尾相接：上一 `final` 的 `end_offset` 是下一句的最早起点。
- 空文本的完结（纯静音端点）不发送 `final`。

### 3.4 `stopped`

```json
{"type": "stopped", "finals": 3}
```

仅在响应 `stop` 时发送，随后服务端关闭连接。

### 3.5 `error`（随后关闭连接）

```json
{"type": "error", "code": "bad_message", "message": "…"}
```

错误码：

| code | 语义 |
|---|---|
| `bad_protocol` | protocol 版本不支持 |
| `bad_message` | 非法消息/帧（如首帧不是 start、二进制长度非偶、未知 type） |
| `unsupported_format` | rate/channels 不支持 |
| `overloaded` | 超出并发上限，稍后重试 |
| `internal` | 识别器内部错误 |
| `idle_timeout` | 空闲超时（冲刷尾部语句后再报错关闭） |

## 4. 断线、恢复与幂等语义

- 连接中断 = 该源会话终止；服务端不保留会话状态。客户端重连即**新会话**（偏移从 0 重新计）。
- 客户端在重连成功后应把断点视为显式 `gap`：之前未完结语句由旧连接的冲刷（或丢失）界定，不伪造连续时间轴。
- 服务端崩溃/重启不影响客户端采集（客户端本地仍留存整场音频）；字幕最多丢失断线期间在途的未完结语句。
- 同一客户端的 mic / loopback 是两条独立连接与会话，互不影响。

## 5. 部署基线

- 单进程 `stt-server serve`，CPU（int8 模型），每会话一个识别器实例 + 可配线程数；并发上限默认 8。
- 模型为 sherpa-onnx 流式 Zipformer（zh-en-punct int8，2026-06-05 版），下载地址与 SHA-256 见 `stt-server/README.md`，不进 Git。
- 与 llm-gateway 并列部署；健康检查 `GET /healthz`（HTTP，返回 `{"status":"ok","model":…,"sessions":N}`）。

## 6. 示例会话

```text
C> {"type":"start","protocol":1,"source_id":"mic","rate":16000,"channels":1}
S< {"type":"started","protocol":1,"model":"sherpa-…"}
C> <binary: 3200 samples>
C> <binary: 3200 samples>
S< {"type":"partial","text":"你好","start_offset":0,"end_offset":6400}
C> <binary: …>            （静音 1.2s 后端点触发）
S< {"type":"final","text":"你好会议","start_offset":0,"end_offset":48000}
C> {"type":"gap"}
C> <binary: …>
S< {"type":"final","text":"新的一句","start_offset":48000,"end_offset":80000}
C> {"type":"stop"}
S< {"type":"stopped","finals":2}
（连接关闭）
```
