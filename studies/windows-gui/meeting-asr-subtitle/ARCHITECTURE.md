# 本地会议字幕系统 — 架构说明

> 版本：v8.7（2026-11-13）
> 定位：飞书会议旁路采集 → 本地离线识别 → Web 实时字幕，全程不上传音频。

---

## 1. 目标与约束

| 目标 | 说明 |
|---|---|
| 本地离线 | SenseVoice / 声纹 / VAD 全部在本机 CPU 运行，音频不离开电脑 |
| 低干扰 | 进程级环回只抓飞书声音，不混入微信、通知、浏览器 |
| 双通道 | 远端（飞书进程环回）+ 本地（自己的麦克风） |
| 可调 | 识别、断句、声纹参数页面可改，大部分实时生效 |
| 可回退 | 每次改动前备份，`CHANGELOG.md` 记录版本 |

约束：

- Windows 10 2004+ / Windows 11
- 需要飞书桌面客户端（进程环回与 UI 自动化都依赖它）
- CPU 推理，建议 4 核以上；模型常驻内存约 400~500MB

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                         浏览器 (static/index.html)                │
│   设备选择 / 开始识别 / 飞书进程 / 参数面板 / 上传音频 / 字幕列表    │
└───────────────┬──────────────────────────────────────────────────┘
                │ HTTP / WebSocket
┌───────────────▼──────────────────────────────────────────────────┐
│ main.py  FastAPI (127.0.0.1:8066)                                 │
│   AppState：会话管理（main / remote / local）                      │
│   Hub     ：跨线程消息队列 → WebSocket 广播                        │
│   REST    ：/api/status /api/devices /api/params /api/transcribe   │
└───────┬───────────────────────┬───────────────────────┬──────────┘
        │                       │                       │
┌───────▼────────┐   ┌──────────▼─────────┐   ┌─────────▼─────────┐
│ RecognitionSession │ RecognitionSession │   │ 上传转写            │
│ 远端 remote       │ 本地 local          │   │ transcribe_samples │
└───────┬────────┘   └──────────┬─────────┘   └─────────┬─────────┘
        │                       │                       │
┌───────▼─────────┐   ┌─────────▼──────────┐            │
│ ProcessLoopback │   │ AudioCapture       │            │
│ 只抓飞书进程音频 │   │ 默认麦克风          │            │
│ 2.5s 分段       │   │ 30ms 回调           │            │
└───────┬─────────┘   └─────────┬──────────┘            │
        │ 16k mono float32      │                       │
        └───────────┬───────────┘                       │
                    ▼                                   │
        ┌───────────────────────────┐                   │
        │ 每个会话独立 VAD (Silero) │                   │
        │ 静音断句 → 语音段         │                   │
        └─────────────┬─────────────┘                   │
                      ▼                                 │
        ┌───────────────────────────┐                   │
        │ SherpaEngine              │◄──────────────────┘
        │  ├ SenseVoice 离线识别     │
        │  ├ 仅中英文过滤            │
        │  └ 3dspeaker 声纹聚类      │
        └─────────────┬─────────────┘
                      ▼
              final / status 事件 → Hub → WebSocket → 页面
```

---

## 3. 三种工作模式

### 3.1 设备模式（`mode="sherpa"`）

页面「开始识别」，选择声卡输入或「系统输出回环」：

```
设备/回环 → AudioCapture/LoopbackCapture → 重采样 16k → VAD → SenseVoice → 字幕
```

适合没有飞书、或想抓整个系统声音的场景。

### 3.2 飞书进程模式（`mode="process"`）

页面「飞书进程」，同时启动两个识别会话：

```
remote: 飞书进程音频（AUDIOCLIENT_PROCESS_LOOPBACK）
        → 2.5s 分段录 WAV → 读 PCM → 16k → VAD → SenseVoice → 说话人N
local : 默认麦克风
        → 飞书静音检测（UI Automation）
        → 开麦时送入 VAD/SenseVoice，标签「本人」
        → 静音或状态未知时丢弃（可关闭 follow_feishu_mute）
```

远端只会收到飞书**输出**的声音（别人说话、共享音频），不会收飞书麦克风输入；
本地通道才是自己的麦克风。

### 3.3 上传模式（`POST /api/transcribe`）

上传 WAV 原始字节 → 解析/重采样 → 独立 VAD 切句 → SenseVoice → 返回 JSON。
用于会后补转写。

---

## 4. 模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | FastAPI 应用、REST/WebSocket、`AppState` 会话管理、`Hub` 广播、selftest 入口 |
| `asr_engine.py` | `SherpaEngine`（模型加载、VAD、识别、声纹、参数）、`RecognitionSession`（采集→切句→识别→广播） |
| `audio_capture.py` | 设备枚举、WASAPI 回环、采样率协商、重采样、PortAudio 输入流、默认麦克风 |
| `process_loopback.py` | Windows 进程级环回采集（分段录 WAV、增量读取、飞书 PID 发现/轮换） |
| `feishu_mute.py` | UI Automation 读取飞书会议参会人列表，判断自己是否静音 |
| `static/index.html` | 单页前端：设备、参数、字幕、上传、状态 |
| `models/` | SenseVoice、3dspeaker、Silero VAD |
| `backup.ps1` | 改动前的版本备份脚本 |
| `CHANGELOG.md` | 版本记录与回退指引 |
| `API.md` / `PARAMS.md` | 接口文档 / 参数手册 |

---

## 5. 线程与并发模型

```
主线程        uvicorn event loop
              ├─ Hub.dispatcher：消费 asyncio.Queue → WebSocket 广播
              ├─ REST 处理（params 修改走 asyncio.to_thread）
              └─ WS 命令处理

会话线程      RecognitionSession._run（每个通道一个）
              ├─ 从 _audio_q 取 30~100ms 音频块
              ├─ 自己的 Silero VAD（engine.new_vad()）
              ├─ 句尾调用 engine.process_segment()
              └─ 停止时 drain VAD

采集线程
              ├─ AudioCapture：PortAudio 回调
              ├─ LoopbackCapture：soundcard 线程
              ├─ ProcessLoopbackCapture：分段录制线程
              └─ FeishuMuteDetector：UIA 轮询线程（0.3s）

识别锁        engine._decode_lock
              ├─ 串行化 SenseVoice decode、声纹提取、参数重建
              └─ 保证双通道同时识别时模型对象不被并发访问
```

跨线程事件统一走 `Hub.emit()`：`loop.call_soon_threadsafe(queue.put_nowait)`，
由 event loop 里的 dispatcher 广播，避免在采集线程直接操作 WebSocket。

---

## 6. 识别管线细节

### 6.1 SenseVoice

- 模型：`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17` int8
- 非流式：一句话必须等 VAD 断句后整句识别，没有逐字 partial
- 输出：文本、语种标签；ITN 开启时数字/日期转书面形式
- 参数：`language`（auto/zh/en）、`use_itn`、`num_threads`；改动会重建识别器

### 6.2 VAD 与断句

- Silero VAD，窗口 512，16k
- `vad_threshold`：语音概率阈值
- `vad_min_silence`：静音多久算一句结束（v6 默认 0.5s）
- `vad_min_speech`：短于该时长丢弃（默认 0.25s）
- `max_segment_seconds`：长发言强制切段（默认 30s）
- **每个通道独立 VAD**，避免双通道互相 reset

### 6.3 声纹

- 3dspeaker ERes2Net，512 维 embedding
- 每句用 VAD 裁掉首尾静音后提取 embedding
- 与已注册说话人做余弦相似度，阈值 `speaker_threshold`（默认 0.45）
- 命中则复用标签；否则满足 `min_register_seconds` 时注册「说话人N」
- 本地麦克风通道不做声纹，固定「本人」

### 6.4 仅中英文过滤

开启 `zh_en_only` 时，输出前丢弃：

1. 模型判定为 ja/ko/yue 的片段
2. 含日文假名（ぅ-ゖ、ァ-ヶ）或韩文谚文（가-힣）
3. 纯符号/无中文或英文字母的噪声

该过滤只影响输出，不改变模型识别本身。

### 6.5 飞书静音联动

`feishu_mute.py` 用 Windows UI Automation 检测「自己」的麦克风状态，不做图像识别。
按以下优先级取第一个可用信号：

1. **悬浮工具栏** `MiniDialogMicrophoneButton`
   - `打开麦克风` → 当前静音；`关闭麦克风` → 当前开麦
2. **完整会议窗口参会人列表**里自己那一行
   - 用 Windows 显示名（可用 `FEISHU_SELF_NAME` 覆盖）匹配
   - 行内按钮名称同上
3. **自己的视频小窗** `ParticipantViewMicrophoneButton` / `ParticipantMicrophoneIcon`
4. 小尺寸悬浮窗内任意 `打开/关闭麦克风` 控件
5. 全部失败 → 未知

状态机：

- 防抖：连续 2 次轮询一致才切换
- 保持：短暂读不到时沿用最近一次已知状态 8 秒
- 未知：本地麦克风**放行**，并在页面提示，避免误停
- 会议窗口消失超过 **10 秒**：自动停止本地通道（释放麦克风），远端继续；
  重新进会需重新启动进程模式
- 静音：本地通道丢弃音频并 reset VAD；开麦后恢复
- `follow_feishu_mute` 可关闭联动；`force_local_mic` 可强制本地开麦（优先级最高）

检测来源（`source`）会写入 `/api/status.mic_mute`：`floating_toolbar`、
`participant_row`、`participant_view`、`small_window`、`hold`、`unknown`。

### 6.6 麦克风自动切换（路线 A：轮询 + 重建）

- 后台每 1 秒用 soundcard 重新枚举麦克风和默认输入
- **跟随 Windows 默认麦克风**：默认设备变化稳定 2 秒 → 自动切到该默认设备
  - 插入 USB/蓝牙、手动改默认、拔掉旧设备都会触发
  - 没有默认麦克风 → 停止本地通道（远端继续）
  - 默认仍指向已消失的绑定设备 → 等待 5 秒后停止
  - 枚举完全为空 → 暂不动作，避免误停
- 切换流程：只停止 local → 先按名称在现有 PortAudio 列表解析；找不到才执行
  `sd._terminate/_initialize` 刷新并重试 → 新建 local 会话
- **按设备名称打开**，避免热插拔后 PortAudio 索引失效（`device ID out of range`）
- 切换失败优先恢复旧设备，避免本地通道直接丢失
- 并发：`AppState.device_lock` 串行化手动 start/stop 与自动切换
- 只在飞书进程模式生效；设备模式（main）不自动切换
- 切换/停止事件写入 `mic_events.log`

限制：

- 蓝牙 HFP/A2DP 切换可能产生多次事件，依赖 2 秒防抖
- USB 接收器仍插着、只拔耳机时，Windows 不移除端点，不会触发
- 每次切换会中断 1~2 秒本地音频
- 只在 Windows 默认设备变化时生效；飞书内部选麦不改变系统默认时检测不到

---

## 7. 参数系统

`asr_engine.PARAM_SPECS` 是唯一参数定义源，页面、REST、WS 共用：

- `type`：float / int / bool / choice
- `apply`：
  - `实时`：下一块音频生效（声纹阈值、最短声纹/注册时长、仅中英文、跟随静音）
  - `重建 VAD`：重新创建 VAD（阈值、断句、最短语音、单句上限）
  - `重建模型`：重新加载 SenseVoice（语言、ITN、线程数，约 4~6 秒）

`SherpaEngine.set_param()` 负责类型校验、范围截断和重建；页面上的滑块和
数字输入都会调用同一接口。

---

## 8. 接口层

### REST

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 状态、通道、句数、说话人、参数、模型、静音状态 |
| GET | `/api/devices` | 输入设备 + 系统回环 + 推荐项 |
| GET | `/api/params` | 参数定义与当前值 |
| POST | `/api/params` | 修改一个或多个参数 |
| POST | `/api/transcribe` | 上传 WAV 原始字节，返回整段转写 |

### WebSocket `/ws`

客户端命令：`start / stop / clear / devices / reset_speakers / status /
get_params / set_param / set_threshold`。

服务端事件：`status / final / level / vad / speakers / params / devices /
clear / warning / error`。

详见 `API.md`。

---

## 9. 前端

单文件 `static/index.html`：

- 设备下拉、刷新、开始/停止、飞书进程、清空、重置说话人
- 参数面板：滑块 + 数字输入（可精确录入），动态从 `/api/params` 渲染
- 上传识别：文件选择 → `POST /api/transcribe` → 按句渲染
- 底部状态：状态、设备、句数、说话人、模型、**麦克风（绑定设备）**、
  **默认麦（系统当前默认）**、飞书麦、提示
- WebSocket 断线自动重连

---

## 10. 模型与文件布局

```
meeting-asr-subtitle/
├── main.py
├── asr_engine.py
├── audio_capture.py
├── process_loopback.py
├── feishu_mute.py
├── static/index.html
├── requirements.txt
├── start.bat / stop.bat / setup.bat
├── backup.ps1
├── README_INSTALL.md
├── API.md / PARAMS.md / ARCHITECTURE.md / CHANGELOG.md
└── models/
    ├── sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/
    │   ├── model.int8.onnx     # 识别模型（约 228MB）
    │   ├── tokens.txt          # 词表
    │   └── test_wavs/          # 自测音频
    ├── 3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx  # 声纹
    └── silero_vad.onnx         # 语音活动检测
```

模型加载顺序：`ensure_loaded()` 查找 SenseVoice 目录 → 构建识别器 → 构建
VAD → 构建声纹提取器。首次加载约 4~6 秒，之后常驻内存。

---

## 11. 关键延迟

| 环节 | 典型耗时 |
|---|---|
| 进程环回分段 | 2.5s（固定窗口） |
| VAD 断句静音 | 0.5s（默认） |
| SenseVoice 识别 | 约音频时长的 0.25~0.3（2 线程） |
| 远端字幕整体延迟 | 约 4~6 秒 |
| 本地麦克风字幕 | 约 2~3 秒 |

进程环回由于 `process-audio-capture` 只能写文件，无法拿到实时帧，采用
「录 2.5s → 停止 → 读取」的方式，段间约 50ms 空隙，实测约 2% 音频损失。

---

## 12. 已知限制

1. **自己是谁靠 Windows 显示名匹配**：换机器/换账号/昵称不一致时可能匹配失败
   （可设 `FEISHU_SELF_NAME` 覆盖；手动指定「我是」的 UI 暂缓）
2. **静音状态未知时放行**：会议窗口最小化、参会人面板关闭、飞书 UI 改版时
   本地通道继续工作，可能把环境噪声送进字幕；可关闭 `follow_feishu_mute`
   或打开 `force_local_mic` 明确控制
3. **不支持空格 PTT 临时发言**：只识别点击静音/取消静音
4. **进程环回分段损失**：约 2% 音频，段边界可能切字
5. **无热词**：当前 sherpa-onnx 的 SenseVoice 绑定不支持热词参数，专有名词
   只能靠后处理替换表
6. **单实例、无鉴权**：服务只监听 `127.0.0.1`，不要暴露到公网
7. **模型固定**：不支持在线学习；文本对只能用于生成替换表/调参
8. **会议窗口最小化超过 10 秒会自动停止本地通道**：需要保持会议窗口可见；
   重新进会需重新启动「飞书进程」。浏览器标签页不会被误判为会议窗口

---

## 13. 版本管理

- 每次改动前：`backup.ps1 -Tag vN -Note "说明"` 生成代码+文档压缩包（不含模型）
- `CHANGELOG.md` 记录每个版本的备份文件名、SHA256、改动内容、回退命令
- 当前版本：v8.7（跟随 Windows 默认麦克风 + 按名称打开 + 版本显示）
- 主要版本：v7 多信号静音检测；v8 会议窗口消失自动停本地通道；
  v8.1 浏览器标签页误判修复；v8.3 麦克风设备显示；v8.4 连接/默认麦显示；
  v8.5 麦克风消失自动切换；v8.7 跟随默认麦克风变化、按名称打开设备
- 便携包：`meeting-asr-subtitle_portable_v8.7_20261113.zip`（含模型）

---

## 14. 运行与部署

```powershell
python -m pip install -r requirements.txt
python main.py
# 或双击 start.bat
# 访问 http://127.0.0.1:8066
```

依赖：`sherpa-onnx`、`sounddevice`、`soundcard`、`process-audio-capture`、
`uiautomation`、`fastapi`、`uvicorn`、`numpy`、`websockets`。
