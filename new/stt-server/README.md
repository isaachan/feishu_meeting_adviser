# stt-server

流式 STT 服务：sherpa-onnx 流式 Zipformer（zh-en-punct int8）经 WebSocket 对外提供识别会话。协议契约见 [CONTRACT_STT_STREAM](../docs/CONTRACT_STT_STREAM.md)（v1）。客户端为 `local-meeting-recorder` 的 `stt: remote-stream` 模式；本模块不依赖仓库内其他模块。

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

- 每个音频源一条 WebSocket 连接（如 mic + loopback = 2 个会话）；`--max-sessions` 需 ≥ 同时在线客户端数 × 2。
- CPU 参考：int8 模型每会话 RTF ≈ 0.1（2 线程）；8 会话 ≈ 需要约 2–4 核富余。
- 首版无认证（内部部署基线，见契约第 1 节）；绑定内网或用防火墙限制来源。

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
