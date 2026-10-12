# feishu_meeting_adviser

## 会中 `.vemedia` → 流式 STT

输入管线：增量解析 AAC → 常驻 FFmpeg 解码为 16 kHz 单声道 PCM16 → 默认每 200 ms 一块。通过统一 STT 会话接口（`scripts/stt_providers.py`）选择 `mock` 或本地 `local`；不改动输入解析和分块。`MockSTT` 仅返回 `[MOCK]` 文本回执，**不是识别结果**。

```bash
python3 -m pip install -r requirements.txt
python3 scripts/stream_stt_mock.py "/path/to/growing.vemedia" --stt-provider mock
```

## M1 本地流式中文／英文 STT

使用 sherpa-onnx 的**流式** Zipformer Transducer（CPU，默认 2 线程），模型状态跨 PCM 块保持；文本变化标记为 `partial`，端点或结束时标记为 `final`。模型本地运行，不上传音频。首次启动需下载官方模型（约 128 MiB 压缩包，解压约 175 MiB），不纳入 Git：

```bash
python3 -m pip install -r requirements-local.txt
mkdir -p models
curl -fL --retry 2 -o /tmp/feishu-streaming-model.tar.bz2 \
  'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05.tar.bz2'
# GitHub 官方 release asset 的 SHA-256，校验不通过请勿解压：
echo 'fa5f63d618e5a01526e275a358bb7772e403f84808a4769fba52cffd8160bf74  /tmp/feishu-streaming-model.tar.bz2' | shasum -a 256 -c -
tar xjf /tmp/feishu-streaming-model.tar.bz2 -C models
python3 scripts/stream_stt_mock.py "/path/to/growing.vemedia" \
  --stt-provider local \
  --local-model-dir models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05
```

可用 `--local-threads 2` 调整推理线程；参数和模型版本需在对比测试中记录。此模型已用其官方中英混说 WAV 在当前 M1 环境完成连续 PCM 输入及临时／最终结果验证；**仍需真实会议测试准确率、CPU／内存与转写延迟**。后续阿里云实现应注入同一接口，不应修改 `.vemedia` 解析器。

## 运行、日志和限制

默认每 100 ms 轮询新字节；PCM 不足 200 ms 时最多等待 300 ms（`--max-chunk-wait-ms`），结束时也会冲刷余量。源文件消失 10 秒后结束；截断／替换报错。输出目录在 `material/stt_<provider>_<时间戳>/`，包含 `events.jsonl`（`transcript` 事件中 `partial` 可以被修订，只有 `final` 才可进入稳定上下文）及 `pcm_s16le_16k_mono.raw`（无文件头）。目录已被 Git 忽略，请按敏感会议资料处理。

静态烟测（**不证明会中实时性**）与自动测试：

```bash
python3 scripts/stream_stt_mock.py material/double_click_20261008140038.vemedia --replay
python3 -m unittest discover -s tests -v
```

本地模型实际选型仍需与云端 STT 用同一批获准使用的音频公平比较，不能据此证明 **6 秒端到端**目标。首次接入真实云端 STT 时还需解决服务端发送速率、鉴权、临时／确认结果及断线重连。
