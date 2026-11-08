# meeting-listener

负责机器人入会、离会、官方字幕轮询和本地 JSONL 字幕采集。所需客户端、配置、存储及清理实现由模块独立维护，不导入 advisor。

## 安装与配置

以下命令在 `meeting-listener/` 执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,stt]'
```

只使用官方字幕时可省略 `stt` extra。仅在没有本机配置时复制 `config.example.yaml` 为 `config.yaml`，填写应用凭据并执行 `chmod 600 config.yaml`。`storage.directory` 默认配置为 `../data`，与 advisor 指向同一数据目录并使用相同应用身份。

本模块有自己的配置，采集参数见 `collector`、`runtime` 和 `http`。为保持入会介绍行为，仍读取 `llm.backend`、`llm.introduction` 和 `notifications.meeting_comment`：配置模型后端且启用介绍和会中消息时，入会后发送一次介绍。介绍和发送记录由 advisor 的 `replies` / `resolve-reply` 检查和处理。

## 运行

```bash
./join <九位会议号或完整入会链接>
./collect
# 停止 collect 后，如需离会
.venv/bin/python -m meeting_listener leave
```

脚本使用本模块的 `.venv`、`config.yaml`。`join` 脚本先重置旧会话；直接 `.venv/bin/python -m meeting_listener join <meeting>` 会核对旧 bot 状态，不进行例行重置。链接中的 `pwd` 自动使用，也可传 `--password` 覆盖。

`collect` 默认实时打印新入库字幕、说话人和时间，`--no-text` 关闭正文输出。停止采集不会离会。CLI 还提供 `status`、`reset-session`、`conflicts`、`resolve-conflict`、`prune` 和 `delete-meeting-data`。

## 本地录制与 STT

```bash
./collect-local /path/to/growing.vemedia
# 预取模型
./collect-local --download-model
```

解析、解码和 STT 代码位于 `scripts/`；使用本模块的 `.venv`，不依赖研究目录的代码或环境。模型在 `models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05`，缺失时从官方 release 下载并验证 SHA-256。

只将最终识别结果交给 `collect --captions -`，音频留在本机。混音没有可靠的说话人区分，统一使用 `--speaker` 标签；时间由录制创建时刻和音频偏移换算，可用 `--recording-start` 覆盖。文件和标准输入也可直接通过 `collect --captions <PATH|->` 输入 JSONL；本地输入和官方事件轮询不能同时运行。

## 开发与文档

```bash
.venv/bin/ruff check src scripts tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s reference/spike/tests -v
```

- [输入契约](docs/TRANSCRIPT_SOURCE_CONTRACT.md)、[API 契约](docs/API_CONTRACT_NOTES.md)
- [采集验证](docs/STAGE1_VALIDATION.md)、[真实会议回归](docs/STAGE1_MEETING_REGRESSION.md)
- [字幕延迟调研](docs/TRANSCRIPT_LATENCY_INVESTIGATION.md)、[TODO](TODO.md)
- [数据操作](../data/docs/DATA_OPERATIONS.md)、[联合运行](../README.md)
