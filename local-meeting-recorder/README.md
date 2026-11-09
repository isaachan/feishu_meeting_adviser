# local-meeting-recorder

在终端用户 laptop 上运行，把本地会议音频转为字幕并交接给 advisor。提供两种模式，输出同一契约事件：

- **fake 模式**：按预写台词及时间间隔回放（无音频依赖，标准库即可运行）。
- **real 模式**（Mac，2026-11-09 起）：ScreenCaptureKit 采集飞书 App 音频（loopback，远端）+ 可选麦克风（本机，默认关闭），经 VAD 分段后调用智谱 GLM-ASR 转写。架构与选型见 [plan.md](../plan.md) 阶段 5。

- 交接契约（事件格式、spool/ack、幂等与恢复）：[recorder 输入契约](../docs/CONTRACT_RECORDER_INPUT.md)
- 会话由 advisor 创建（`advisor session-new`），本模块只引用 `session_id`

## 安装

以下命令在 `local-meeting-recorder/` 执行（fake 模式仅标准库；real 模式需安装 `sounddevice` 依赖与 Xcode Command Line Tools）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## fake 回放

```bash
.venv/bin/recorder fake examples/standup.jsonl --session <session_id> \
  [--data-root ../data] [--speed 10]
```

- 台词为 JSONL：`{"speaker": "张三", "text": "……", "delay_ms": 1500}`；`speaker` 可为 null，`delay_ms` 是该句之前的间隔。
- 回放启动时刻即会话时间轴原点；`--speed` 等比缩短等待（时间戳仍按原间隔换算，见契约第 5 节）。
- 事件按契约写入 `<data-root>/recorder/spool/<session>/<stream>/`；advisor 消费后写回 ack 游标，本模块据此清理已确认段。
- 正常结束发送 `stream_end`；Ctrl-C 干净停止且**不**发送 `stream_end`（已写入事件保留，等待 advisor 确认）。
- `--data-root` 默认指向仓库 `data/`（开发用途）；其他位置显式传入。

## real 采集（Mac）

```bash
.venv/bin/recorder real --session <session_id> [--mic] [--duration 600] \
  [--mic-speaker 本机] [--data-root ../data]
```

- **权限**：宿主终端 App 需要系统设置 → 隐私与安全性中的「屏幕录制」（ScreenCaptureKit loopback，采集飞书系进程音频）与「麦克风」（仅 `--mic` 时）；首次授权后需重启终端 App。
- **隐私（手动开关）**：麦克风默认**关闭**；`--mic` 显式开启（本工具不检测飞书静音状态，静音期间请自行关闭）。
- **STT**：智谱 GLM-ASR（`glm-asr-2512`），API key 取自 `GLM_ASR_API_KEY` 或 `GLM_API_KEY` 环境变量；分段上限 25s（API 限 30s/25MB），转写失败重试后丢弃该段并在 summary 记录，不中断采集。
- **留存**：整场原始音频（int16 wav）与事件日志保存在 `<data-root>/recorder/audio/<stream_id>/`，**不自动删除**，请手动清理。
- `--duration N` 到期后干净停止并写 `stream_end`；Ctrl-C 中断不写（语义与 fake 一致）。
- 首次运行会用 `swiftc` 编译采集助手（缓存于临时目录）；打包版本通过 `LOCAL_MEETING_RECORDER_SCK_BIN` 使用预编译二进制。

## 开发与测试

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

跨模块链路验证（fake-recorder → advisor）在根 [integrations/](../integrations/README.md)。
