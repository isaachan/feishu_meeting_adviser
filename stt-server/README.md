# stt-server

流式 STT 服务：经 WebSocket 对外提供识别会话，支持两类引擎（启动时按 `--model-dir` 内容自动选择，`started`/healthz 里的 `model` 即目录名）：

- **流式 Zipformer transducer**（zh-en-punct int8）：partial + final，内置端点检测；
- **FireRedASR2 AED**（`sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26`，非流式）：silero VAD 切段后整段解码，**仅 final 无 partial**；段落=VAD 停顿边界（最长 15s 强切），输出**无标点**（根 TODO T13）。

协议契约见 [CONTRACT_STT_STREAM](../legacy/docs/CONTRACT_STT_STREAM.md)（v1，方案 B 遗留、方案 C 继续沿用，auth 固定 `none`）。客户端为 `legacy/local-meeting-recorder` 的 `stt: remote-stream` 模式与现根目录 `recorder`；本模块不依赖仓库内其他模块。

## 安装（Ubuntu Server，CPU）

```bash
cd stt-server
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 下载模型（约 128 MiB，不进 Git；校验失败请勿解压）
mkdir -p models
curl -fL --retry 2 -o /tmp/stt-model.tar.bz2 \
  'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05.tar.bz2'
echo 'fa5f63d618e5a01526e275a358bb7772e403f84808a4769fba52cffd8160bf74  /tmp/stt-model.tar.bz2' | sha256sum -c -
tar xjf /tmp/stt-model.tar.bz2 -C models
```

## 运行

```bash
.venv/bin/stt-server serve --host 0.0.0.0 --port 8392 \
  --model-dir models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05 \
  --threads 2 --max-sessions 8
# 健康检查: curl http://127.0.0.1:8393/healthz
```

切换到 FireRedASR2 只需把 `--model-dir` 指向该模型目录（本地栈即改 `scripts/local.env` 的 `STT_MODEL_DIR`），服务自动选引擎。FireRedASR2 额外需要 `silero_vad.onnx` 放在同目录（官方 GitHub release `asr-models/silero_vad.onnx`；网络不通时可用 modelscope 镜像 `xnnehang/k2-fsa-silero-vad`，实测 sha256 `9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6`，2026-11-18 本机冒烟通过：22.8s 测试音频 5 个 final、文本与离线 demo 一致、sherpa-onnx 1.13.8 兼容）。注意 sherpa 的 silero VAD 必须按 512 样本窗口喂（session 内已处理），大块直喂会丢帧。RTF 实测对比（同机同音频、2 线程笔记本 CPU，2026-11-19）：流式 transducer ≈0.043（输出带标点、英文小写），FireRedASR2 ≈0.17（无标点、英文全大写，慢约 4 倍），容量规划按此折算。

- 每个音频源一条 WebSocket 连接（如 mic + loopback = 2 个会话）；`--max-sessions` 需 ≥ 同时在线客户端数 × 2。
- CPU 参考：int8 模型每会话 RTF ≈ 0.1（2 线程）；8 会话 ≈ 需要约 2–4 核富余。
- 首版无认证（内部部署基线，见契约第 1 节）；绑定内网或用防火墙限制来源。

### CPU/解码耗时测量

在运行 stt-server 的机器、仓库根目录执行（生产环境使用该模块的 venv）：

```bash
# 会议进行中：被动观察 30 秒
.venv-prod/stt-server/bin/python scripts/measure-stt.py live --seconds 30

# 会后：用保存的录音按实时速度回放最多 60 秒
.venv-prod/stt-server/bin/python scripts/measure-stt.py replay --wav /path/to/loopback.wav

# 暂无录音文件：只测 60 秒静音基线，不能代表真实语音负载
.venv-prod/stt-server/bin/python scripts/measure-stt.py replay
```

本地栈可将上述 Python 路径换成 `.venv-local/bin/python`。录音须为 16 kHz、单声道、16-bit PCM WAV（recorder 保存的 `loopback.wav` 符合此格式），且文件须位于运行脚本的机器上。回放直接连本机 STT，不经过 advisor；检测到已有 STT 会话时会拒绝回放。输出只含会话数、进程 CPU、音频秒数、解码耗时及 RTF，不显示识别文本。自动找不到唯一进程时可在 `live`/`replay` 前加 `--pid <PID>`。CPU 平均占用按 100% = 一个逻辑核计算；解码 RTF 是解码耗时 / 音频时长，不包含模型加载及全部音频处理开销。

## systemd 示例

`/etc/systemd/system/stt-server.service`：

```ini
[Unit]
Description=Streaming STT server (sherpa-onnx)
After=network.target

[Service]
User=stt
WorkingDirectory=/opt/stt-server
ExecStart=/opt/stt-server/.venv/bin/stt-server serve --host 0.0.0.0 --port 8392 \
  --model-dir /opt/stt-server/models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## 开发与测试

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

测试使用注入的假识别器（无需模型）；服务回环覆盖 start/音频/gap/stop/错误码/并发上限。
