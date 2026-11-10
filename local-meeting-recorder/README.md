# local-meeting-recorder

在终端用户 laptop 上运行，把本地会议音频转为字幕并交接给 advisor。提供两种模式，输出同一契约事件：

- **fake 模式**：按预写台词及时间间隔回放（无音频依赖，标准库即可运行）。
- **real 模式**（Mac，2026-11-09 起）：ScreenCaptureKit 采集飞书 App 音频（loopback，远端）+ 可选麦克风（本机，默认关闭），流式转写落字幕。默认 STT 为**本地 sherpa-onnx 流式模型**（2026-11-10 起；智谱 GLM-ASR 降为可选，Coding Plan 无 ASR 配额）。架构与选型见 [plan.md](../plan.md) 阶段 5。

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
.venv/bin/python -m pip install -e '.[local]'   # sherpa-onnx + numpy（本地 STT）
.venv/bin/recorder real --session <session_id> [--mic] [--duration 600] \
  [--mic-speaker 本机] [--stt sherpa|zhipu] [--stt-model-dir <dir>] [--data-root ../data]
```

- **权限**：宿主终端 App 需要系统设置 → 隐私与安全性中的「屏幕录制」（ScreenCaptureKit loopback，采集飞书系进程音频）与「麦克风」（仅 `--mic` 时）；首次授权后需重启终端 App。
- **隐私（手动开关）**：麦克风默认**关闭**；`--mic` 显式开启（本工具不检测飞书静音状态，静音期间请自行关闭）。
- **STT（默认 remote-stream 远端流式）**：远端 `stt-server`（见 [stt-server](../stt-server/README.md) 与 [流式 STT 契约](../docs/CONTRACT_STT_STREAM.md)）承担识别，客户端零本地 STT 计算；`--stt-url` 或 `LMR_STT_URL` 指定服务地址（如 `ws://stt.internal:8392`），未配置时报错——或改用 `--stt sherpa` 本地流式（`--stt-model-dir`/`LMR_STT_MODEL_DIR` 指定模型目录，模型下载见下）。
- **STT（可选 zhipu 文件上传）**：`--stt zhipu`（glm-asr-2512，API key 取 `GLM_ASR_API_KEY`/`GLM_API_KEY`；GLM Coding Plan 无 ASR 配额，需单独计费资源）；分段上限 25s（API 限 30s/25MB），转写失败重试后丢弃该段并在 summary 记录。
- sherpa 模型下载（remote-stream/sherpa 共用同一模型；约 128 MiB，不进 Git，SHA-256 见 [stt-server](../stt-server/README.md)）：
  ```bash
  curl -fL --retry 2 -o /tmp/stt-model.tar.bz2 \
    'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05.tar.bz2'
  echo 'fa5f63d618e5a01526e275a358bb7772e403f84808a4769fba52cffd8160bf74  /tmp/stt-model.tar.bz2' | shasum -a 256 -c -
  mkdir -p models && tar xjf /tmp/stt-model.tar.bz2 -C models
  export LMR_STT_MODEL_DIR=$PWD/models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05
  ```
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
