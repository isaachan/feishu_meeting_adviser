# Recorder 输入与本地交接契约

> **本契约已废弃（方案 B 遗产）。** recorder↔advisor 交接在方案 C 中改为网络流式契约（[new/docs/CONTRACT_RECORDER_STREAM.md](../new/docs/CONTRACT_RECORDER_STREAM.md)，WS 上行 + 流令牌），本地的 spool/ack 文件交接不再使用；其幂等语义（event_id/sequence/stream_end）已平移进新契约。现行实现在 `new/recorder/` 与 `new/advisor/`，唯一开发入口见 [new/README.md](../new/README.md)。

状态：阶段 0 冻结（2026-11-08）；2026-11-10 增补 real 模式会议绑定元数据（第 6 节，向后兼容，不改事件格式）。`local-meeting-recorder` 与 `advisor` 各自按本文件实现与测试；两侧不得互相导入源码，契约变更需同步修改两侧实现并更新根 `integrations/` 用例。背景见 [plan.md](../plan.md)。

## 1. 范围与模块入口（plan 0.1）

| 模块 | 首版入口 | 自有数据 |
|---|---|---|
| `local-meeting-recorder` | `recorder fake <script.jsonl> --session <session_id>`（仅 fake 模式；真实声卡采集暂缓） | `local-meeting-recorder/data/`：台词解析与回放进度、输出流 outbox（未确认事件） |
| `advisor` | 本地会话与消费命令（`session` / `consume`，名称以实现为准） | 仓库根 `data/`：本地会话、字幕源、上下文、任务与决定 |

- 会话由 advisor 创建并持久化；recorder 只引用 `session_id`。
- 一次 recorder 启动产生一个 `stream_id`；进程重启后是新 stream，续传旧 stream 的未确认事件。
- 两模块仅通过共享数据根下的交接目录（第 3 节）与本契约交互。

## 2. 事件格式（plan 0.2）

交接单位为 JSONL 事件，一行一个对象。首版只有两种 `kind`。

### 2.1 `caption`（最终字幕）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | int | 是 | 当前为 `1` |
| `session_id` | str | 是 | advisor 本地会话标识 |
| `stream_id` | str | 是 | 本输入流标识，格式 `<session_id>:<uuid4>` |
| `sequence` | int | 是 | 流内严格递增，从 1 开始 |
| `event_id` | str | 是 | `"<stream_id>:<sequence>"`，全局唯一 |
| `kind` | str | 是 | `caption` |
| `utterance_id` | str | 是 | 句子稳定身份，流内不重复 |
| `revision` | int | 是 | 句子修订号，首版恒为 `1`；`>1` 表示可核实的修订 |
| `text` | str | 是 | 最终字幕正文，去首尾空白后非空，≤2000 字符 |
| `speaker_label` | str \| null | 否 | 发言人标签；无法区分时为 `null`，不得默认等于录制者 |
| `start_offset_ms` / `end_offset_ms` | int | 是 | 相对会话时间轴的起止毫秒，`end >= start >= 0` |
| `wall_clock_at` | str | 是 | 事件产生的本机时间（RFC3339），仅供诊断，不参与新鲜度判断 |

- ASR partial 中间结果不交接；只交接最终字幕。
- 同一 `utterance_id` 的文本变化以更高 `revision` 的新事件表达，由 advisor 按既有修订/冲突规则处理。

### 2.2 `stream_end`

携带 `last_sequence`（该流最后事件的 `sequence`），无字幕正文。recorder 正常停止或台词回放结束时发送。

## 3. 本地交接传输

目录约定位于共享本机数据根（仓库根 `data/recorder/`）：

```text
data/recorder/spool/<session_id>/<stream_id>/000001.jsonl   # recorder 追加写入的事件段
data/recorder/ack/<session_id>/<stream_id>.json             # advisor 写入的确认游标
```

- 段文件只追加；写满或每 ≤2 秒滚动新段；先写 `.tmp` 再原子重命名为最终段名（段名为该段起始 `sequence`，六位零填充）。
- `ack` 文件内容为 `{"acked_sequence": <N>}`：advisor 在事件**持久化事务提交之后**原子更新，`N` 是已连续确认的最大前缀。
- recorder 依据 ack 前缀清理已确认段；未确认事件在 recorder 重启后原样保留重投。

## 4. 幂等、顺序与恢复

- advisor 按 `event_id` 去重：重复读取的段不重复进入字幕源与上下文。
- 单流内按 `sequence` 顺序消费：出现缺口时等待补齐，不跳过；确认游标只推进连续前缀。
- 跨流不要求全局顺序，advisor 按 `start_offset_ms`（辅以 `wall_clock_at`）归并进会话上下文。
- `stream_end` 被确认到 `last_sequence` 后，该流标记为 drained。
- 会话结束由用户在 advisor 侧显式触发；单个流结束不自动结束会话。
- 崩溃恢复：recorder 重投未确认事件；advisor 从 ack 游标继续；两侧均不依赖内存状态。

## 5. 时间语义

- fake 回放以**回放启动时刻**为会话时间轴原点，台词时间间隔映射为 offset；加速回放按比例缩放 offset，保持相对时序。
- advisor 的新鲜度与证据窗口基于会话时间轴（offset + 会话开始时间），不得使用剧本文件中的历史日期。
- `wall_clock_at` 只用于诊断与跨流排序兜底。

## 6. 会话与数据承接（plan 0.5）

- 本地会话使用独立标识空间（`local:` 前缀 + UUID），与既有飞书会议数据并存；旧数据保留可查，不清库、不迁移。
- 飞书链路已停用：不再从本契约之外的来源创建新会话。
- real 模式录制绑定经飞书组织者授权核验选定的日历会议（授权语义见 [STT 流契约](CONTRACT_STT_STREAM.md) 第 2.1 节）：recorder 将 `calendar_event_id` 以流级元数据随 spool 交接（须在该流字幕事件被消费前可读），advisor 将其与会话关联保存，用于把字幕对应到飞书会议。fake 模式与未绑定会议的流缺省该字段；此增补不改变事件字段与 `schema_version`。

## 7. 示例

正常（节选）：

```jsonl
{"schema_version":1,"session_id":"local:ab12","stream_id":"local:ab12:cd34","sequence":1,"event_id":"local:ab12:cd34:1","kind":"caption","utterance_id":"u-001","revision":1,"text":"我们开始过项目状态。","speaker_label":"张三","start_offset_ms":1200,"end_offset_ms":4100,"wall_clock_at":"2026-11-08T10:00:01+08:00"}
{"schema_version":1,"session_id":"local:ab12","stream_id":"local:ab12:cd34","sequence":2,"event_id":"local:ab12:cd34:2","kind":"caption","utterance_id":"u-002","revision":1,"text":"后端联调本周完成。","speaker_label":"李四","start_offset_ms":4400,"end_offset_ms":6900,"wall_clock_at":"2026-11-08T10:00:04+08:00"}
{"schema_version":1,"session_id":"local:ab12","stream_id":"local:ab12:cd34","sequence":3,"event_id":"local:ab12:cd34:3","kind":"stream_end","last_sequence":3,"wall_clock_at":"2026-11-08T10:00:05+08:00"}
```

- **重复交接**：recorder 重投段 1；advisor 按 `event_id` 去重，ack 仍只推进连续前缀。
- **断开恢复**：advisor 崩溃后从 `{"acked_sequence":1}` 继续，重新读段 2、3；recorder 在 ack 覆盖前不清理段文件。

## 8. 版本

`schema_version=1`。不兼容变更递增版本号，并同时更新两侧实现、测试与根 `integrations/` 用例后方可切换。
