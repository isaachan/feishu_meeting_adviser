# 本地会议字幕服务 — 接口与参数文档

> 服务地址：`http://127.0.0.1:8066`
> 识别模型：`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`（非流式，中/英/日/韩/粤）
> 所有识别均在本机离线完成，音频不上传外网。

## 1. 快速开始

```bash
# 查看当前状态
curl http://127.0.0.1:8066/api/status

# 上传一段 WAV 转写
curl -X POST --data-binary @meeting.wav \
     -H "Content-Type: audio/wav" \
     http://127.0.0.1:8066/api/transcribe
```

---

## 2. 可调参数

参数通过 `PARAM_SPECS` 统一定义，HTTP 和 WebSocket 共用。标「实时」的参数改完立即影响下一句识别；「重建 VAD / 重建模型」会重新创建对应对象（约几十毫秒 / 4~5 秒）。

| 参数名 | 页面名称 | 类型 | 默认值 | 范围 / 可选值 | 生效方式 | 说明 |
|---|---|---|---|---|---|---|
| `speaker_threshold` | 声纹阈值 | float | `0.45` | 0.30 ~ 0.95 | 实时 | 声纹相似度阈值。越低越容易合并成同一人，越高越容易拆成新说话人 |
| `min_embed_seconds` | 最短声纹时长(s) | float | `0.6` | 0.2 ~ 3.0 | 实时 | 语音短于该时长不做声纹比对，沿用上一个说话人 |
| `min_register_seconds` | 最短注册时长(s) | float | `1.0` | 0.5 ~ 5.0 | 实时 | 语音短于该时长不会注册新说话人，避免短句/噪声造人 |
| `vad_threshold` | VAD 阈值 | float | `0.5` | 0.10 ~ 0.90 | 重建 VAD | 语音检测概率阈值。调高更保守，调低更灵敏 |
| `vad_min_silence` | 断句静音(s) | float | `0.5` | 0.2 ~ 3.0 | 重建 VAD | 静音超过该时长就切句。越小出字幕越快，但容易切碎 |
| `vad_min_speech` | 最短语音(s) | float | `0.25` | 0.05 ~ 2.0 | 重建 VAD | 短于该时长的声音会被丢弃，用于过滤噪声 |
| `max_segment_seconds` | 单句上限(s) | float | `30.0` | 5.0 ~ 60.0 | 重建 VAD | 单句最长时长，超过强制切段输出 |
| `language` | 识别语言 | choice | `auto` | `auto` / `zh` / `en` | 重建模型 | 指定语言可略微提升准确率 |
| `zh_en_only` | 仅中英文 | bool | `true` | true / false | 实时 | 丢弃日/韩/粤及含假名/谚文/纯符号的片段 |
| `follow_feishu_mute` | 跟随飞书静音 | bool | `true` | true / false | 实时 | 飞书静音时暂停本地麦克风通道；状态未知时放行；会议窗口消失 10 秒自动停止本地通道 |
| `force_local_mic` | 强制本地开麦 | bool | `false` | true / false | 实时 | 忽略飞书静音与自动停止，始终采集本地麦克风（优先级最高） |
| `use_itn` | 数字规整(ITN) | bool | `true` | true / false | 重建模型 | 把「五十」转成「50」、「二零二六年」转成「2026年」 |
| `num_threads` | 识别线程数 | int | `2` | 1 ~ 8 | 重建模型 | 推理线程数。越大越快，但更占 CPU |

环境变量 `SPEAKER_THRESHOLD` 可指定启动时的声纹阈值默认值，例如：

```bash
SPEAKER_THRESHOLD=0.50 python main.py
```

超出范围的数值会被自动截断到边界；类型错误返回 400 / WebSocket `warning`。

---

## 3. HTTP 接口

所有响应均为 JSON，字符集 UTF-8。

### 3.1 `GET /api/status`

返回服务与当前会话状态。

```json
{
  "state": "idle",
  "mode": "",
  "version": "v8.7",
  "device_id": null,
  "loopback_id": null,
  "sentences": 0,
  "channels": {},
  "speakers": [],
  "mic_device": null,
  "default_mic": null,
  "mic_mute": {
    "muted": null,
    "known": false,
    "detail": "未启动",
    "source": "none",
    "in_meeting": false,
    "updated_at": 0
  },
  "speaker_threshold": 0.45,
  "params": {
    "speaker_threshold": 0.45,
    "min_embed_seconds": 0.6,
    "min_register_seconds": 1.0,
    "vad_threshold": 0.5,
    "vad_min_silence": 0.5,
    "vad_min_speech": 0.25,
    "max_segment_seconds": 30.0,
    "language": "auto",
    "zh_en_only": true,
    "use_itn": true,
    "num_threads": 2
  },
  "model": {}
}
```

`model` 在模型加载后才有内容，形如：

```json
{
  "asr_kind": "sense_voice",
  "asr_model": "model.int8.onnx",
  "asr_dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
  "language": "auto",
  "use_itn": true,
  "num_threads": 2,
  "embedding_dim": 512,
  "load_seconds": 4.45
}
```

### 3.1.1 `GET /api/version`

返回当前版本：

```json
{ "version": "v8.7" }
```

### 3.2 `GET /api/devices`

列出音频输入设备（含系统输出回环）。

```json
{
  "devices": [
    {
      "id": "{0.0.0.00000000}.{53a658be-...}",
      "name": "扬声器 (Realtek(R) Audio)",
      "hostapi": "WASAPI Loopback",
      "channels": 1,
      "default_samplerate": 48000.0,
      "kind": "loopback"
    }
  ],
  "suggested": { "...": "同上，推荐的默认设备" }
}
```

`kind` 为 `loopback` 时，`id` 是字符串，需原样传回；为 `input` 时是整数设备号。

### 3.3 `GET /api/params`

返回全部参数定义与当前值。

```json
{
  "specs": {
    "speaker_threshold": {
      "label": "声纹阈值",
      "type": "float",
      "default": 0.45,
      "min": 0.3,
      "max": 0.95,
      "step": 0.01,
      "desc": "声纹相似度阈值……",
      "apply": "实时"
    }
  },
  "values": { "speaker_threshold": 0.45 }
}
```

### 3.4 `POST /api/params`

修改一个或多个参数。

单个参数：

```bash
curl -X POST http://127.0.0.1:8066/api/params \
  -H "Content-Type: application/json" \
  -d '{"name":"vad_min_silence","value":0.6}'
```

批量：

```bash
curl -X POST http://127.0.0.1:8066/api/params \
  -H "Content-Type: application/json" \
  -d '{"speaker_threshold":0.42,"language":"zh","use_itn":true}'
```

成功响应：

```json
{
  "updated": { "vad_min_silence": 0.6 },
  "values": { "...": "全部参数当前值" }
}
```

失败响应（HTTP 400）：

```json
{ "error": "language: language 只能是 ['auto', 'zh', 'en']" }
```

### 3.5 `POST /api/transcribe`

上传一整段音频做离线转写。**请求体是 WAV 原始字节**，不是 multipart 表单。

要求：

- 仅支持 16-bit PCM WAV
- 单/多声道均可（多声道自动混成单声道）
- 任意采样率，服务端自动重采样到 16 kHz

```bash
curl -X POST --data-binary @meeting.wav \
     -H "Content-Type: audio/wav" \
     http://127.0.0.1:8066/api/transcribe
```

响应：

```json
{
  "text": "派放时间早上9点至下午5点。",
  "segments": [
    { "t0": 0.8, "t1": 5.59, "text": "派放时间早上9点至下午5点。", "spk": "说话人1" }
  ],
  "speakers": ["说话人1"],
  "duration": 5.59,
  "language": "auto"
}
```

说明：

- 服务端用 VAD 自动切句，每段独立识别一次
- 说话人编号在该次请求内独立聚类（不会污染实时会话的说话人）
- 当前参数（VAD、语言、ITN、声纹阈值等）对该请求生效
- 首次调用需要加载模型，可能多花 4~5 秒

错误响应（HTTP 400 / 500）：

```json
{ "error": "WAV 解析失败：仅支持 16-bit PCM WAV" }
```

---

## 4. WebSocket 接口

地址：`ws://127.0.0.1:8066/ws`

连接建立后服务端先推送一条 `status` 消息（含完整 `params`）。之后双向收发 JSON。

### 4.1 客户端 → 服务端

| cmd | 字段 | 说明 |
|---|---|---|
| `start` | `mode`: `"sherpa"` / `"process"`；设备模式需 `kind` + `device`；进程模式可选 `pid`、`mic` | 开始识别（设备 / 飞书进程环回） |
| `stop` | — | 停止识别 |
| `clear` | — | 清空前端字幕 |
| `devices` | — | 刷新设备列表 |
| `reset_speakers` | — | 清空已注册说话人（需先停止） |
| `status` | — | 查询状态 |
| `get_params` | — | 获取参数定义与当前值 |
| `set_param` | `name`、`value` | 修改单个参数 |
| `set_threshold` | `value` | 兼容旧接口，等价于 `set_param("speaker_threshold")` |

示例：

```json
{"cmd":"start","mode":"sherpa","kind":"loopback","device":"{0.0.0.00000000}.{53a658be-...}"}
{"cmd":"start","mode":"process"}
{"cmd":"start","mode":"process","pid":1234,"mic":false}
{"cmd":"set_param","name":"vad_threshold","value":0.45}
{"cmd":"set_param","name":"language","value":"zh"}
```

### 4.2 服务端 → 客户端

| type | 字段 | 说明 |
|---|---|---|
| `status` | `state`: `idle`/`loading`/`running`/`stopped`/`error`；`msg`；`sentences`；`speakers`；`params`；`model`；`mic_device` | 状态变更 |
| `mic` | `device`: `{id, name, hostapi, running, connected, current_name}`；`default`: `{name, id}` | 当前麦克风连接状态 / 系统默认输入变化 |
| `final` | `text`、`spk`、`t0`、`t1`、`index` | 一句识别结果 |
| `level` | `v`（0~1） | 输入电平，约 10 次/秒 |
| `vad` | `speaking` | 是否检测到语音 |
| `speakers` | `names` | 当前说话人列表 |
| `params` | `specs`、`values` | 参数定义与当前值 |
| `devices` | `devices`、`suggested` | 设备列表 |
| `clear` | — | 清空字幕 |
| `warning` / `error` | `msg` | 警告 / 错误 |

说明：当前使用非流式 SenseVoice 模型，**不产生 `partial` 事件**，每句话在静音断句后整句输出 `final`。

`final` 示例：

```json
{"type":"final","text":"开饭时间早上9点至下午5点。","spk":"说话人1","t0":0.77,"t1":5.59,"index":1}
```

### 4.3 Python 客户端示例

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8066/ws") as ws:
        await ws.send(json.dumps({"cmd": "set_param", "name": "language", "value": "zh"}))
        await ws.send(json.dumps({
            "cmd": "start", "mode": "sherpa",
            "kind": "loopback", "device": "{0.0.0.00000000}.{...}",
        }))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "final":
                print(msg["spk"], msg["text"])

asyncio.run(main())
```

### 4.4 飞书进程环回模式

`{"cmd":"start","mode":"process"}` 使用 Windows 的 `AUDIOCLIENT_PROCESS_LOOPBACK`，
**只抓飞书进程输出的声音**，微信、通知、浏览器视频等其它软件的声音不会混进来。

- 自动查找正在播放音频的飞书进程（优先窗口标题含「会议」的进程）
- 有多个飞书进程时，连续静音 10 秒会自动轮换到下一个候选
- 也可以传 `pid` 指定目标进程
- `mic`（默认 `true`）同时开启本地麦克风通道，用于识别「本人」发言
- 本地麦克风通道跟随飞书静音状态（参数 `follow_feishu_mute`，默认开）：
  飞书里静音时暂停本地识别，取消静音后自动恢复；远端通道不受影响
- `/api/status` 的 `mic_mute` 字段返回 `{muted, known, detail, source, in_meeting, updated_at}`
- **`known=false` 时本地麦克风放行**（不再默认静音），避免误停
- 静音检测信号（纯 UI Automation，按优先级）：
  1. 悬浮工具栏 `MiniDialogMicrophoneButton`（`打开麦克风`=静音，`关闭麦克风`=开麦）
  2. 完整会议窗口参会人列表里自己那一行
  3. 自己的视频小窗 `ParticipantViewMicrophoneButton` / `ParticipantMicrophoneIcon`
  4. 小尺寸悬浮窗内任意 `打开/关闭麦克风` 控件
  5. 全部失败 → 未知 → 放行
- 会议窗口必须是飞书进程（`feishu.exe` / `lark.exe`）自己的窗口；浏览器标签页
  （如标题含「飞书会议」的 Chrome 页）不参与判断
- 自己身份目前按 **Windows 显示名** 匹配，可用环境变量 `FEISHU_SELF_NAME` 覆盖；
  换机器/昵称不一致时可能匹配失败，此时 `known=false`、放行
- 参数 `force_local_mic=true` 可完全忽略静音检测，始终采集本地麦克风
- `/api/status` 的 `mic_device` 是本地通道**绑定的**麦克风：`connected=false` 表示设备已断开，
  `running=false` 表示未在采集；`default_mic` 是 Windows **当前默认输入**
- **自动切换**：Windows 默认麦克风变化稳定 2 秒 → 自动切到该默认设备并重建本地采集流。
  插入 USB/蓝牙、手动改默认、拔掉旧设备都会触发；没有默认麦克风 → 停止本地通道。
  远端进程环回不受影响
- 切换时按**设备名称**打开，不依赖可能失效的 PortAudio 索引；切换失败优先恢复旧设备
- 只在飞书进程模式生效；只在飞书内部切麦克风、不改 Windows 默认时检测不到
- 切换事件写入 `mic_events.log`，便于排查
- **会议窗口消失超过 10 秒**：自动停止本地麦克风通道（释放麦克风设备），远端
  进程环回继续运行；重新进会需要再次 `start mode=process`
- 自动停止仅在「跟随飞书静音」开启且「强制本地开麦」关闭时生效

双通道：

| 字段 | 值 | 说明 |
|---|---|---|
| `channel` | `remote` | 飞书进程环回，远端参会人 |
| `channel` | `local` | 本地麦克风，说话人固定为「本人」 |
| `spk` | `说话人N` / `本人` | 远端通道做声纹聚类；本地通道不打标 |

`final` 示例：

```json
{"type":"final","text":"这个方案可以。","spk":"说话人2","channel":"remote","t0":3.1,"t1":6.4,"index":5}
{"type":"final","text":"我同意。","spk":"本人","channel":"local","t0":3.5,"t1":5.2,"index":1}
```

限制与注意：

- 需要 Win10 2004+ / Win11；部分域控电脑可能被组策略禁用该 API
- 底层按 2.5 秒分段录制，段之间约 50ms 间隙（实测约 2% 音频损失）
- 字幕延迟约 4~6 秒（分段 + 断句 + 识别）
- Windows 系统总音量拉到 0 仍能抓到声音；但在飞书里把扬声器静音后抓不到
- 同时在用扬声器外放时，麦克风通道可能把远端声音也收进去（回声），建议戴耳机
- 依赖 `process-audio-capture`

---

## 5. 常见问题

**没有 `partial` / 字幕不是逐字出现？**
SenseVoice 是非流式模型，实时模式按 VAD 断句整句输出。想更快出字幕，把 `vad_min_silence` 调小（如 0.3）。

**说话人标签太多或太少？**
调 `speaker_threshold`：标签太多就调低（0.40~0.45），不同人被合并就调高（0.48~0.55）。改完点「重置说话人」。

**上传返回 400？**
确认是 16-bit PCM WAV。很多录音软件默认导出 32-bit float 或 mp3，需要先转换。

**修改 `language` / `use_itn` / `num_threads` 后短暂卡顿？**
这三个参数需要重新加载识别器（约 4~5 秒），属于正常现象。
