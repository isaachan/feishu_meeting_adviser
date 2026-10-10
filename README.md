# feishu_meeting_adviser

## 会中音频 → 模拟 STT

目前只实现输入管线，不调用真实 STT，也**不识别讲话内容**：增量读取 `.vemedia` → 解析完整 AAC 音频包 → 常驻 FFmpeg 解码、重采样为 16 kHz 单声道 PCM16 → 以可配置时长分块发给 `MockSTT`。Mock 返回 `[MOCK] received ...` 文本回执，不是真实转写。

```bash
python3 -m pip install -r requirements.txt
python3 scripts/stream_stt_mock.py "/path/to/growing.vemedia" --target-chunk-ms 200
```

默认每 100 ms 检查新字节，PCM 目标分块 200 ms；不足一块的音频最多等 300 ms（`--max-chunk-wait-ms`），关闭时也会冲刷余量。源文件消失 10 秒后自动结束；文件截断或被替换则报错而非悄悄拼接。输出位于启动日志中的 `material/stt_mock_<时间戳>/`：`events.jsonl` 记录 AAC 接收、模拟 STT 回执、停止原因，`pcm_s16le_16k_mono.raw` 供核对（无文件头）。输出目录不纳入 Git。

离线烟测（**不能证明会中延迟**）：

```bash
python3 scripts/stream_stt_mock.py material/double_click_20261008140038.vemedia --replay
python3 -m unittest discover -s tests -v
```

注意：初次接入真实 STT 时须适配它的流式音频格式、连接生命周期、临时/确认文本以及流控；当前 Mock 回执不代表转写成功，也不能证明 6 秒端到端目标。
