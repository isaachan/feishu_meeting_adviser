# Fire Red ASR2（AED, int8）模型可用性验证（2026-11-18）

目标：验证 `~/models/sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26` 能否在本机用 sherpa-onnx 正常离线识别。

## 结论

**可用。**sherpa-onnx 2.0.1（pip，本目录 `.venv`）加载成功，模型自带 `test_wavs/` 全部 8 条识别正确，包括：

- 中英混说（0/1/2.wav，英文全大写输出，如 `昨天是 MONDAY TODAY IS礼拜二 ...`）
- 方言（四川 3-sichuan / 天津 4-tianjin / 河南 5-henan）
- 8kHz 音频（demo 内线性插值重采样到 16k 后识别正常）

注意点：

1. sherpa-onnx 2.x 的 `OfflineRecognizer` 不再接受平铺 kwargs，需走类方法 `OfflineRecognizer.from_fire_red_asr(encoder=..., decoder=..., tokens=..., ...)`（AED 版；CTC 版另有 `from_fire_red_asr_ctc`，本模型不适用）。
2. `stream.accept_waveform(sample_rate, waveform)` 必须显式传采样率，模型要求 16kHz。
3. 仅支持单声道 16-bit PCM wav；无标点输出（对比 SenseVoice 无 ITN 选项）。

## 复现

```bash
studies/fire-red-asr2/.venv/bin/python studies/fire-red-asr2/demo.py            # 跑模型自带 test_wavs
studies/fire-red-asr2/.venv/bin/python studies/fire-red-asr2/demo.py foo.wav    # 或指定 wav
```

依赖：`sherpa-onnx==2.0.1`、`numpy`（已装在 `.venv`，重建：`python3 -m venv studies/fire-red-asr2/.venv && studies/fire-red-asr2/.venv/bin/pip install sherpa-onnx numpy`）。

## 补充确认：语言与热词（2026-11-18）

官方文档：

- FireRedAsr 专页：<https://k2-fsa.github.io/sherpa/onnx/FireRedAsr/index.html>（含本模型 `sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26` 的下载/解码命令；注明 "supports Chinese and English"，普通话 + 20 多种方言）
- 热词文档：<https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html>

结论：

1. **语言无需也无法配置**：该模型本身就是 zh+en 双语模型，`OfflineRecognizer.from_fire_red_asr()` 没有 `language` 参数（不同于 SenseVoice），不存在"只 enable 中英文"的开关——它天然只识别中英文。
2. **热词（contextual biasing）不支持**：官方明确 "Only transducer models support hotwords … All other models don't"。FireRedASR AED 仅支持 `greedy_search` 解码，`from_fire_red_asr()` 也没有 `hotwords_file`/`hotwords_score` 参数。替代：
   - 输出后处理：拼音词组匹配替换（homophone-replacer，模型无关）<https://k2-fsa.github.io/sherpa/onnx/homophone-replacer/index.html>
   - 若必须原生热词，需换 transducer 类模型（streaming/offline zipformer transducer + `modified_beam_search`）。

