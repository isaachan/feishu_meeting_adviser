# local-meeting-recorder

> **方案 B 遗产（已停维，仅作移植参考）。** 已被方案 C 的 [`new/recorder/`](../../new/recorder/) 取代：去 spool/本地 OAuth/STT 客户端，改 WS 直推云端 advisor ingest（流令牌鉴权），采集层（SCK 双路 + MuteGate + 本地留存）已平移。本模块不再维护。唯一开发入口见 [new/README.md](../../new/README.md)。

在终端用户 laptop 上运行，把本地会议音频转为字幕并交接给 advisor。提供两种模式，输出同一契约事件：

- **fake 模式**：按预写台词及时间间隔回放（无音频依赖，标准库即可运行）。
- **real 模式**（Mac，2026-11-09 起）：ScreenCaptureKit 采集飞书 App 音频（loopback，远端）+ 可选麦克风（本机，默认关闭），流式转写落字幕。默认 STT 为 **remote-stream 远端流式**（stt-server 承担识别，需 `--stt-url`）；本地 sherpa-onnx（`--stt sherpa`）与智谱 GLM-ASR（`--stt zhipu`，Coding Plan 无 ASR 配额，需单独计费资源）为可选。架构与选型见 [plan.md](../plan.md) 阶段 5。

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
.venv/bin/python -m pip install -e '.[local]'   # 仅本地 sherpa STT 需要（sherpa-onnx + numpy）
.venv/bin/recorder real --session <session_id> [--mic] [--duration 600] \
  [--mic-speaker 本机] [--stt remote-stream|sherpa|zhipu] [--stt-url ws://host:8392] \
  [--stt-model-dir <dir>] [--data-root ../data]
```

- **权限**：宿主终端 App 需要系统设置 → 隐私与安全性中的「屏幕录制」（ScreenCaptureKit loopback，采集飞书系进程音频）与「麦克风」（仅 `--mic` 时）；首次授权后需重启终端 App。
- **隐私（手动开关）**：麦克风默认**关闭**；`--mic` 显式开启（本工具不检测飞书静音状态，静音期间请自行关闭）。
- **STT（默认 remote-stream 远端流式）**：远端 `stt-server`（见 [stt-server](../stt-server/README.md) 与 [流式 STT 契约](../docs/CONTRACT_STT_STREAM.md)）承担识别，客户端零本地 STT 计算；`--stt-url` 或 `LMR_STT_URL` 指定服务地址（如 `ws://stt.internal:8392`），未配置时报错——或改用 `--stt sherpa` 本地流式（`--stt-model-dir`/`LMR_STT_MODEL_DIR` 指定模型目录，模型下载见下）。
- **STT（可选 zhipu 文件上传）**：`--stt zhipu`（glm-asr-2512，API key 取 `GLM_ASR_API_KEY`/`GLM_API_KEY`；GLM Coding Plan 无 ASR 配额，需单独计费资源）；分段上限 25s（API 限 30s/25MB），转写失败重试后丢弃该段并在 summary 记录。
- sherpa 模型下载（仅 `--stt sherpa` 需要，客户端才下载；与 stt-server 服务端共用同一模型，约 128 MiB，不进 Git，SHA-256 见 [stt-server](../stt-server/README.md)）：
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

## 飞书组织者授权与会议选择（契约 v2）

stt-server 以 `--auth feishu` 运行时，只有所选日历会议**组织者**的 user_access_token 能建立识别会话（契约见 [流式 STT 契约](../docs/CONTRACT_STT_STREAM.md) v2）。CLI 全流程：

```bash
# 1) 凭据：环境变量或模块 data 目录下的 config.json（{"app_id":…,"app_secret":…}，不进 Git）
export LMR_FEISHU_APP_ID=cli_xxx LMR_FEISHU_APP_SECRET=xxx
# 2) 用户授权（浏览器本地回调，redirect URI 需与飞书应用一致，默认 http://127.0.0.1:8765/oauth/callback）
.venv/bin/recorder auth login
.venv/bin/recorder auth status
# 3) 列出当前时间窗（±30 分钟）内可录制的会议；仅 [组织者] 条目会被服务端接受
.venv/bin/recorder meetings
# 4) 绑定会议开始采集：--event 携带上面列出的 event_id
.venv/bin/recorder real --session <session_id> --stt-url ws://host:8392 \
  --event '<calendar_id>/<event_id>'
```

- `--event` 会随流写入 spool `meta.json`（契约 §6 会议绑定），并在 remote-stream 模式作为 `start.auth` 送 stt-server 核验；`auth_expired` 由客户端静默刷新重连，用户无感。
- 即时会议（无日历事件）不支持；组织者/时间窗的最终判定在服务端（fail-closed），本列表只是客户端提示。
- 真实 API 形状已于 2026-11-10 核验（`event_organizer.user_id` 值域 open_id、`user_info` 返回 open_id/union_id、`event_id` 纯 UUID 客户端复合、无效 token code 20005→`auth_expired`），证据 `data/acceptance-auth/`；`auth login` 需要飞书应用开通日历读取权限与上述 redirect URI。

## 开发与测试

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

跨模块链路验证（fake-recorder → advisor）在根 [integrations/](../integrations/README.md)。
