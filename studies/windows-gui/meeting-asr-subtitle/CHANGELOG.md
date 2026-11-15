# 版本变更记录

> 项目：本地会议字幕 / 飞书进程音频识别
> 约定：**每次改动代码前先备份**，并在本文件追加一条版本记录。
> 备份目录：`C:\Users\pengw.wang\.claude\projects\`
> 备份只包含代码和文档，不包含 `models/`（模型体积大且不随代码变化）。

---

## 版本一览

| 版本 | 日期 | 备份文件 | 大小 | SHA256 前 8 位 | 主要内容 |
|---|---|---|---|---|---|
| v0 | 2026-11-04 16:03 | `meeting-asr-subtitle_流式.zip` | 103 MB | `0338F79D` | 最早的流式 zipformer + pyannote 版本（含模型） |
| v1 | 2026-11-04 19:22 | `meeting-asr-subtitle.zip` | 196 MB | `D7D95479` | 切换 SenseVoice 离线识别，新增参数面板、上传接口（含模型） |
| v2 | 2026-11-05 10:12 | `meeting-asr-subtitle_20261105-1012.zip` | 37 KB | `FAC1CA45` | v1 的代码备份 + 文档，稳定基线（与 10:00 备份完全一致） |
| v2.1 | 2026-11-05 13:49 | `meeting-asr-subtitle_recording-experiment_20261105-1349.zip` | 42 KB | `2980C72E` | 飞书 MP4 快照录制方案（实验，已回退，勿使用） |
| v3 | 2026-11-05 15:22 | `meeting-asr-subtitle_ProcessLoopback_20261105-1522.zip` | 42 KB | `CD880D94` | 进程级音频环回 + 双通道（远端/本地）；2026-11-06 曾回退到此版本 |
| v3-b | 2026-11-06 10:17 | `meeting-asr-subtitle_v3-before-zh_en_20261106-1017.zip` | 46 KB | `D21C547F` | 改动前备份：回退后的 v3 原样（含当时文档） |
| v4 | 2026-11-05 18:25 | `meeting-asr-subtitle_ProcessLoopback-zh_en_20261105-1825.zip` | 44 KB | `7A3F5AAA` | 仅中英文 + 声纹质心聚类 + 参数调优（2026-11-06 已回退） |
| v5 | 2026-11-06 10:52 | `meeting-asr-subtitle_v5-zh_en_20261106-1052.zip` | 46.8 KB | `B4526A32` | v3 + 仅中英文过滤（不含质心声纹与参数调优） |
| v6-b | 2026-11-06 11:06 | `meeting-asr-subtitle_v6-before-mute_20261106-1106.zip` | 47.2 KB | `B1A6969B` | 改动前备份：v5 原样 |
| v6 | 2026-11-06 12:33 | `meeting-asr-subtitle_v6-mute-gate_20261106-1233.zip` | 48.9 KB | `88743820` | **当前版本**：v5 + 飞书麦克风静音联动 |
| v7-b | 2026-11-06 12:47 | `meeting-asr-subtitle_v7-before-failopen_20261106-1247.zip` | 49.6 KB | `947DD2A2` | 计划改动前备份：自己身份识别 + 失败放行（**已暂停，代码未改**） |
| v6.1 | 2026-11-06 13:11 | `meeting-asr-subtitle_v6.1-docs-portable_20261106-1311.zip` | 50.2 KB | `29199B36` | 新增架构文档、更新 API、生成便携包（代码未改） |
| v7-a | 2026-11-06 13:45 | `meeting-asr-subtitle_v7-before-selfid_20261106-1344.zip` | 50.7 KB | `01F5BDBE` | 改动前备份：多信号静音检测实施前 |
| v7 | 2026-11-07 11:33 | `meeting-asr-subtitle_v7-selfid-failopen_20261107-1133.zip` | 52 KB | `8D0DCBAA` | 多信号静音检测 + 未知放行 + 强制本地开麦 |
| v8-a | 2026-11-07 11:45 | `meeting-asr-subtitle_v8-before-meeting-stop_20261107-1145.zip` | 52.7 KB | `ED3B7117` | 改动前备份：会议窗口消失自动停本地通道实施前 |
| v8 | 2026-11-07 12:02 | `meeting-asr-subtitle_v8-meeting-auto-stop_20261107-1202.zip` | 53.3 KB | `6406EE2F` | 会议窗口消失 10 秒自动停止本地通道 |
| v8.1 | 2026-11-07 12:41 | `meeting-asr-subtitle_v8.1-window-filter_20261107-1240.zip` | 53.9 KB | `B2BCCA8C` | 修复浏览器标签页被误判为飞书会议窗口 |
| v8.2 | 2026-11-07 15:26 | `meeting-asr-subtitle_v8.2-docs-sync_20261107-1526.zip` | 53 KB | `056E85FD` | 文档同步到 v8.1（架构/API/README/HANDOFF），代码未改 |
| v8.3-b | 2026-11-13 13:18 | `meeting-asr-subtitle_v8.3-before-mic-display_20261113-1318.zip` | 53.3 KB | `3B8CD598` | 改动前备份：显示当前麦克风设备前 |
| v8.3 | 2026-11-13 13:47 | `meeting-asr-subtitle_v8.3-mic-display_20261113-1347.zip` | 53.8 KB | `A0DC0FC5` | 页面底部显示当前使用的麦克风设备 |
| v8.4-b | 2026-11-13 15:36 | `meeting-asr-subtitle_v8.4-before-mic-display-fix_20261113-1535.zip` | 54.3 KB | `A40FF3F6` | 改动前备份：麦克风显示准确性修正前 |
| v8.4 | 2026-11-13 16:30 | `meeting-asr-subtitle_v8.4-mic-display-fix_20261113-1630.zip` | 55.9 KB | `50F8CF74` | 绑定麦克风 + 默认麦 + 断开状态显示 |
| v8.5-b | 2026-11-13 17:20 | `meeting-asr-subtitle_v8.5-before-auto-mic-switch_20261113-1720.zip` | 56.5 KB | `7E55D7C9` | 改动前备份：麦克风自动切换实施前 |
| v8.5 | 2026-11-13 18:12 | `meeting-asr-subtitle_v8.5-auto-mic-switch_20261113-1812.zip` | 57.8 KB | `8A4E2D3B` | 路线 A 轮询 + 重建，麦克风消失自动切换 |
| v8.6-b | 2026-11-13 18:41 | `meeting-asr-subtitle_v8.6-before-no-cache_20261113-1841.zip` | 58.4 KB | `EE588581` | 改动前：页面 no-cache 头 |
| v8.7-b | 2026-11-13 18:59 | `meeting-asr-subtitle_v8.7-before-follow-default_20261113-1859.zip` | 58.5 KB | `C678458C` | 改动前：跟随默认麦克风 |
| v8.7.1-b | 2026-11-13 19:24 | `meeting-asr-subtitle_v8.7.1-before-name-device_20261113-1924.zip` | 59.2 KB | `C978558E` | 改动前：按名称打开设备 |
| v8.7 | 2026-11-13 19:48 | `meeting-asr-subtitle_v8.7-follow-default_20261113-1948.zip` | 59.5 KB | `D6406F6C` | **当前版本**：跟随默认麦 + 按名称打开 + 版本显示 |

> 选择回退目标：日常回退优先用 **v4**；需要进程环回但保留五语种用 **v3**；不需要进程环回、只要 SenseVoice + 参数面板用 **v2**；最早的流式版本用 **v0**。

---

## 各版本详细变化

### v0 — 流式 zipformer（2026-11-04 16:03）

- 识别：`sherpa-onnx-streaming-zipformer-small-ctc-zh-int8`（流式）
- 声纹：pyannote 分割 + 3dspeaker
- Web 实时字幕，逐字 partial
- 无参数面板、无上传接口、无进程环回

### v1 — 切换 SenseVoice（2026-11-04 19:22）

- 识别改为 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`（非流式）
- 改为 VAD 断句 + 整句识别，**不再有逐字 partial**
- 新增 10 项参数面板（滑块 + 手输）和 `/api/params`
- 新增 `/api/transcribe` 上传音频转写（WAV）
- 说话人：3dspeaker 声纹在线聚类
- 新增 `API.md`

### v2 — 稳定基线（2026-11-05 10:12）

- 与 v1 功能一致，补充 `PARAMS.md` 参数手册
- 纯代码备份（不含模型），文件与 10:00 备份逐字节一致
- 新增对转录解码方式的修复：transducer 使用 `modified_beam_search`

### v2.1 — MP4 快照录制实验（2026-11-05 13:49，已回退）

- 方案：轮询飞书本地录制 MP4 → 快照 → ffmpeg 增量抽音频 → 识别
- 结论：飞书写的是 moov 在末尾的普通 MP4，录制中索引不完整，实时不可行；**已回退**
- 保留此备份仅作记录，不要作为回退目标

### v3 — 进程级环回（2026-11-05 15:22）

- 新增 `process_loopback.py`：`AUDIOCLIENT_PROCESS_LOOPBACK`，只抓飞书进程音频
- 双通道：`remote`（飞书进程环回）+ `local`（默认麦克风，标签「本人」）
- 自动查找飞书音频 PID，静音 10 秒轮换候选
- 保留 SenseVoice 五语种（auto/zh/en/ja/ko/yue）
- 声纹仍为旧的「单条 embedding + 固定阈值」

### v4 — 仅中英文 + 声纹优化（2026-11-05 18:25，当前）

相对于 v3 的改动：

| 改动 | v3 | v4 |
|---|---|---|
| 语种选项 | auto/zh/en/ja/ko/yue | **auto/zh/en** |
| 语种默认值 | auto | **zh** |
| 仅中英文过滤 | 无 | **新增 `zh_en_only`**：丢弃日/韩/粤、含假名/谚文、纯符号片段 |
| 声纹算法 | 单条 embedding，命中阈值 0.45 | **质心 + 滞后切换（0.05）** |
| 最短注册时长 | 1.0s | **1.5s** |
| 断句静音 | 0.5s | **0.7s** |
| 最短语音 | 0.25s | **0.3s** |
| 文档 | — | `PARAMS.md`、`API.md` 同步更新 |

### v5 — 仅中英文过滤（2026-11-06 10:52，当前）

在回退后的 v3 基础上，只重新加入「仅中英文」能力，不带回 v4 的其它改动：

- 新增参数 `zh_en_only`（默认开，实时生效）
- `language` 选项收窄为 `auto/zh/en`，默认仍为 `auto`
- 过滤三类结果：
  1. 模型判定为日/韩/粤的片段
  2. 含日文假名或韩文谚文的文本
  3. 纯符号、无中文/英文字母的噪声
- 同步更新 `PARAMS.md`、`API.md`
- 实测：zh/en 保留，ja/ko/yue 丢弃；关闭开关后日文结果恢复

**未包含**（仍在 v4 备份中）：质心声纹聚类、`language=zh` 默认、断句 0.7s、最短语音 0.3s、最短注册 1.5s。

### v6.1 — 架构文档 + 便携包（2026-11-06 13:11，当前）

- 新增 `ARCHITECTURE.md`：总体架构、三种工作模式、线程模型、识别/声纹/静音联动、
  参数系统、模型与延迟、已知限制
- 更新 `API.md`：补充静音联动、`follow_feishu_mute`、`mic_mute`、自己身份识别限制
- 新增 `README_INSTALL.md`、`setup.bat`：目标机器安装说明与一键装依赖
- 生成便携包 `meeting-asr-subtitle_portable_v6_20261106.zip`（194.3 MB，含三个模型）
  - 已解压实测 `--selftest`，识别正常
  - 不含旧流式模型、paraformer、pyannote、ffmpeg-mini
- 代码未改动，行为与 v6 一致

### v7 — 多信号静音检测 + 未知放行（2026-11-07 11:33）

问题：悬浮工具栏布局下，原实现只查完整会议窗口的参会人列表，读不到状态 →
按静音处理 → 取消静音后本地通道仍被拦截。

改动：

- `feishu_mute.py` 重写为多信号检测（纯 UIA，无图像识别），按优先级：
  1. 悬浮工具栏 `MiniDialogMicrophoneButton`
     - `打开麦克风` = 静音；`关闭麦克风` = 开麦
  2. 完整会议窗口参会人列表里自己那一行（Windows 显示名匹配，可用
     `FEISHU_SELF_NAME` 覆盖）
  3. 自己的视频小窗 `ParticipantViewMicrophoneButton` / `ParticipantMicrophoneIcon`
  4. 小尺寸悬浮窗内任意 `打开/关闭麦克风` 控件
  5. 全部失败 → 未知
- 状态机：连续 2 次轮询一致才切换（防抖）；短暂读不到时沿用最近状态 8 秒；
  8 秒后仍未知则放行
- 未知时 **本地麦克风放行**，不再默认静音；状态消息带检测来源
- 新增参数 `force_local_mic`（默认关）：忽略静音检测，强制本地开麦
- `/api/status.mic_mute` 新增 `source`、`in_meeting`、`updated_at`
- 前端「飞书麦」未知时显示「未知（放行）」
- 文档：`PARAMS.md`、`API.md`、`ARCHITECTURE.md` 同步更新

实测：

- 悬浮工具栏按钮名称随静音/取消静音实时变化（探测日志 13:33-13:34 完整记录）
- 未知状态启动进程模式：本地通道放行，提示「静音状态未知，本地麦克风放行」
- 单元测试：known muted 拦截、known open 放行、unknown 放行、强制开麦放行、
  关闭跟随放行

仍待办：手动指定「我是」的页面选择器（本次未做）。

### v8 — 会议窗口消失 10 秒自动停止本地通道（2026-11-07 12:02，当前）

问题：会议结束后本地麦克风仍在采集，环境音继续进字幕。

改动：

- `asr_engine.RecognitionSession`：当静音检测的 `in_meeting=false` 持续
  **10 秒**，自动把本地会话 `running` 置为 False，退出采集线程并释放麦克风
- 仅在「跟随飞书静音」开启且「强制本地开麦」关闭时生效；两个开关任一改变
  都会禁用自动停止
- 远端进程环回通道不受影响，继续运行
- 会话停止事件新增 `channel` 字段；前端对 `channel != main` 的状态只更新提示，
  不影响全局运行状态（避免本地停止导致页面误判整体停止）
- 文档：`PARAMS.md`、`API.md`、`ARCHITECTURE.md` 同步更新

验证：

- 单元测试：`in_meeting=false` 首次放行；超过 10 秒调用后返回 False，
  `running=false`，输出「会议窗口已消失超过 10 秒，停止本地麦克风通道」
- 会议窗口存在时计时器会重置，不会误停

注意：会议窗口最小化超过 10 秒也会触发停止；需要重新点「飞书进程」恢复。

### v8.1 — 修复浏览器标签页误判（2026-11-07 12:41，当前）

现象：会议关闭后超过 10 秒，本地通道仍未停止，继续收音。

根因：

- `feishu_mute._meeting_windows()` 只要窗口标题包含「飞书会议」就当作会议窗口
- Chrome 里打开的标签页标题是 `飞书会议语音转文字功能 - 豆包 - Google Chrome`，
  会议结束后仍然存在
- 因此 `in_meeting` 一直是 `true`，10 秒计时器永远不启动，本地通道继续采集

修复：

- 会议窗口必须属于飞书进程（`feishu.exe` / `lark.exe`），通过窗口 `ProcessId`
  + `QueryFullProcessImageNameW` 校验
- 额外排除浏览器标题（`Google Chrome`、`Microsoft Edge`、`Firefox`、`豆包`）
- 其他逻辑不变

验证：

- 仅有 Chrome 标签页、无真实会议窗口时：`in_meeting=false`
- 启动进程模式后 10 秒输出「会议窗口已消失超过 10 秒，停止本地麦克风通道」
- 本地通道 `running=false`，远端通道继续 `running=true`，页面不误判整体停止

### v8.3 — 页面显示当前麦克风设备（2026-11-13 13:47，当前）

- `audio_capture.describe_device()`：返回设备 id / 名称 / hostapi
- `RecognitionSession`：采集启动/停止时发送 `mic` 事件（`{id,name,hostapi,running}`）
- `AppState`：记录 `mic_device_id`，`/api/status` 新增 `mic_device` 字段
- 页面底部新增「麦克风：设备名」，停止后显示「（已停止）」，鼠标悬停显示 hostapi/id
- 仅显示，不提供切换；会中切换默认麦克风仍需重启会话
- 文档：`API.md`、`ARCHITECTURE.md`、`README_INSTALL.md` 同步

实测：进程模式启动后显示 `麦克风阵列 (适用于数字麦克风的英特尔® 智音技术) · MME`；
停止后 `running=false`。

### v8.7 — 跟随默认麦克风 + 按名称打开（2026-11-13 19:48，当前）

- 规则改为：**Windows 默认麦克风变成谁，本地通道就切到谁**
  - 插入 USB/蓝牙、手动改默认、拔掉旧设备都会触发
  - 默认变化稳定 2 秒后切换
  - 默认设备为空 → 停止本地通道
  - 默认仍指向已消失的绑定设备 → 等待 5 秒后停止
  - 枚举完全为空 → 暂不动作，避免误停
- 切换改为**按设备名称打开**，不再依赖可能失效的 PortAudio 索引
  - 解决拔 USB 后 `device ID out of range` 导致本地通道停止
  - 需要时才刷新 PortAudio，刷新后重试一次
  - 切换失败时优先恢复旧设备，避免本地通道直接丢失
- 页面底部新增「版本」；`/api/status` 和 `/api/version` 返回 `v8.7`
- `index.html` 加 no-cache 响应头，页面更新后普通刷新即可
- 实测：阵列 ↔ Jabra 多次自动切换，本地字幕恢复，远端不受影响
- 便携包（含模型）：`meeting-asr-subtitle_portable_v8.7_20261113.zip`
  （SHA256 见同名 `.sha256` 文件，包外校验，避免自引用）

### v8.5 — 麦克风消失自动切换（路线 A，2026-11-13 18:12）

- 后台每 1 秒检查绑定麦克风是否还在（按名称 + hostapi）
- 绑定设备消失连续 2 秒：
  - 有 Windows 默认麦克风 → 只重启 local 通道到该设备
  - 没有默认麦克风 → 停止 local，remote 继续
- 切换前执行 `sd._terminate()` + `sd._initialize()` 刷新 PortAudio 设备列表
- 新设备按名称解析，优先相同 hostapi，其次 WASAPI
- `AppState.device_lock` 串行化手动 start/stop 与自动切换
- 只在飞书进程模式生效；设备模式不自动切换
- 绑定设备仍在时，即使默认设备变化也不切换
- 切换/停止事件写入 `mic_events.log`
- 修复：soundcard 在工作线程中需要 `CoInitializeEx`，否则枚举为空会被误判为
  “没有默认麦克风”而错误停止本地通道

验证：

- 单元测试：绑定设备消失 → 停止旧 local → 刷新 PortAudio → 新 local 启动（device_id 正确）
- 正常启动进程模式：不再出现“枚举为空导致误停”
- 注意：会议窗口消失超过 10 秒仍会按 v8 规则停止本地通道，这是独立逻辑

### v8.4 — 麦克风显示准确性（2026-11-13 16:30）

问题：USB 麦克风拔出后页面仍显示 USB；无法看出绑定设备和系统当前默认是否一致。

改动：

- 绑定设备改为保存「名称 + hostapi + id」，不再每次按索引重新解析
- 用 `soundcard`（WASAPI）重新枚举当前麦克风，避免 PortAudio 设备列表过期
- 每次状态刷新校验绑定设备是否存在：
  - 存在 → `connected=true`
  - 不存在 → `connected=false`，显示「（已断开）」，`running=false`
  - 枚举失败 → 保持原状态，避免误报断开
- `/api/status` 新增 `default_mic`（Windows 当前默认输入）
- 页面底部新增「默认麦」；绑定麦克风断开时，麦克风/默认麦标黄
- 后台每 3 秒检查一次，通过 `mic` 事件推送变化
- `mic` 事件增加 `default` 字段
- 文档：`API.md`、`ARCHITECTURE.md`、`README_INSTALL.md` 同步

验证：

- 绑定 Jabra、当前列表只有 Intel 阵列 → `connected=false, running=false`
- 绑定 Intel → `connected=true`，`current_name` 正确
- `/api/status` 的 `default_mic` 为 Intel 阵列

注意：本版本只修正显示，**不会自动切换或重启本地通道**；热插拔后仍需重启会话/服务才能真正换到新设备。

### v8.2 — 文档同步（2026-11-07 15:26）

代码未改，仅同步文档到 v8.1 的行为：

- `ARCHITECTURE.md`：版本更新到 v8.1，补充多信号检测、未知放行、
  会议窗口消失自动停止、浏览器标签页排除
- `API.md`：参数表补充 `force_local_mic`、未知放行、自动停止、窗口进程校验
- `README_INSTALL.md`：更新未知放行、自动停止、强制本地开麦、常见问题
- `HANDOFF.md`：从 v0 流式文档重写为 v8.1 当前交接说明
- 便携包同步重建（文档已包含最新版本）

便携包（含模型）：

- `meeting-asr-subtitle_portable_v8.1_20261107.zip`（194.3 MB，含模型）
- SHA256 见同目录 `meeting-asr-subtitle_portable_v8.1_20261107.zip.sha256`
  （避免包内自引用，校验文件在包外）
- 已解压运行 `--selftest`，识别正常

### v6 — 飞书麦克风静音联动（2026-11-06 12:33）

问题：飞书里已经静音、不在会议中发言，但本机麦克风仍开着，会把环境噪声送进本地通道生成字幕。

改动：

- 新增 `feishu_mute.py`：用 Windows UI Automation 读取飞书会议参会人列表
  - 用 Windows 显示名（`GetUserNameExW`）匹配「自己」那一行
  - `打开麦克风` 按钮存在 = 当前静音；`关闭麦克风` = 当前开麦
  - 后台每 0.3 秒轮询，独立线程
- 新增参数 `follow_feishu_mute`（默认开，实时生效）
- 本地通道（`local`）在飞书静音或状态未知时丢弃麦克风音频，并重置 VAD；
  取消静音后自动恢复；远端进程环回通道不受影响
- `/api/status` 新增 `mic_mute: {muted, known, detail}`，页面底部新增「飞书麦」状态
- 依赖新增 `uiautomation==2.0.29`
- `PARAMS.md`、`API.md` 同步更新

已知限制：

- 只识别点击静音/取消静音，不支持空格 PTT 临时发言
- 会议窗口最小化、参会人面板关闭、飞书 UI 改版时识别失败，按静音处理
- 状态未知时本地通道暂停；可关闭 `follow_feishu_mute` 恢复旧行为

实测：

- 会议窗口可见时正确识别 `Peng WANG 王鹏：静音`
- 静音状态下启动进程模式：远端正常 running，本地通道暂停，无本地字幕
- 关闭开关后本地通道恢复
- 状态未知时按静音处理，并输出状态提示

---

## v3 → v4 差异（逐项说明）

1. **只保留中英文**
   - `language` 选项收窄为 `auto/zh/en`，默认 `zh`
   - 新增 `zh_en_only`（默认开）：识别结果若被判为日/韩/粤，或文本含假名（ぁ-ん、ァ-ヶ）、谚文（가-힣），或没有任何中文/英文字母，直接丢弃
   - 实测：zh/en 正常保留，ja/ko/yue 全部过滤

2. **识别准确率调优**
   - `language=zh`：4 段中英混合音频实测，与 auto 持平或略好，纯英文音频也能正确识别
   - `vad_min_silence` 0.5→0.7：减少半句话被切断，给模型更多上下文
   - `vad_min_speech` 0.25→0.3：过滤更短噪声

3. **声纹（说话人）优化**
   - 每个说话人维护归一化质心，按命中次数滑动平均
   - 切换说话人需比当前相似度高 0.05 以上，避免标签抖动
   - `min_register_seconds` 1.0→1.5，短句不再造新说话人
   - 单元测试：单人 10 段始终 1 个标签；两人交替正确新增并切回

---

## 回退记录

### 2026-11-06 10:06 — 从 v4 回退到 v3

- 回退目标：`meeting-asr-subtitle_ProcessLoopback_20261105-1522.zip`
- 原因：用户要求
- 验证：`asr_engine.py`、`main.py`、`audio_capture.py`、`process_loopback.py`、`static/index.html`、`API.md`、`PARAMS.md`、`HANDOFF.md`、`requirements.txt` 共 9 个文件 SHA256 与 v3 备份一致
- 服务重启后确认：`language` 选项恢复为 auto/zh/en/ja/ko/yue，`zh_en_only` 不存在，参数恢复 v3 默认（vad_min_silence=0.5、vad_min_speech=0.25、min_register_seconds=1.0）
- 本次回退丢弃的改动（仍保留在 v4 备份中）：
  - 仅中英文过滤 `zh_en_only`
  - 声纹质心 + 滞后切换
  - language=zh、断句静音 0.7s、最短语音 0.3s、最短注册 1.5s
- `CHANGELOG.md` 和 `backup.ps1` 不在 v3 备份内，回退时予以保留

---

## 相对最早版本（v0）的累计改动

| 维度 | v0（流式） | 当前 v4 |
|---|---|---|
| 识别模型 | streaming zipformer CTC | SenseVoice 多语种离线（int8） |
| 输出方式 | 逐字 partial | VAD 断句后整句输出 |
| 语种 | 中文（CTC） | 中/英（过滤其它） |
| 音频来源 | 声卡 / 系统回环 | 声卡 / 系统回环 / **飞书进程级环回** |
| 双通道 | 无 | **远端 + 本麦克风** |
| 参数配置 | 无 | 11 项，页面可调，部分实时生效 |
| 上传转写 | 无 | **`/api/transcribe`**（WAV） |
| 说话人 | pyannote + 3dspeaker | 3dspeaker + **质心在线聚类** |
| 接口文档 | 无 | `API.md`、`PARAMS.md`、本文件 |

---

## 备份与回退流程

### 改动前（必须）

1. 运行备份脚本（或让 Claude 执行）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\backup.ps1 -Tag v5 -Note "本次改动说明"
   ```

2. 脚本会在上级目录生成 `meeting-asr-subtitle_v5_<时间>.zip`，并打印 SHA256
3. 在本文件「版本一览」表追加一行，在该版本下写详细说明
4. 再开始改代码

### 回退

```powershell
Expand-Archive meeting-asr-subtitle_<版本>_<时间>.zip `
  -DestinationPath C:\Users\pengw.wang\.claude\projects\meeting-asr-subtitle -Force
```

- 模型目录 `models/` 不在备份内，回退不会影响模型
- 回退后重启服务
- 如果当前目录有备份里没有的新文件（例如实验脚本），需要手动删除

### 校验

完整 SHA256：

```
meeting-asr-subtitle_流式.zip
  0338F79D080938F64B24749917E0074500B5D3BA68DDDF27B01FEC968F7C799D

meeting-asr-subtitle.zip
  D7D95479955520B8A764FFBA6C654F4835B18DCC60B297D457C47B4930EDC99F

meeting-asr-subtitle_20261105-1012.zip
  FAC1CA455459C6F79A16CD8969FA05D0DB19457191FAEA3892EBCF39F6A8122B

meeting-asr-subtitle_recording-experiment_20261105-1349.zip
  2980C72EA1A66F644A65E22E8D2E39DC912D5FEAE1719C5D38B1EE5CA2C30C8F

meeting-asr-subtitle_ProcessLoopback_20261105-1522.zip
  CD880D94D24C33FE9965F343EDEF822E4640C85FB9DF2CA2E46FE3479B1317B7

meeting-asr-subtitle_ProcessLoopback-zh_en_20261105-1825.zip
  7A3F5AAAEFAD1815E3A61C1DD07B8B4D12A65AA49C4F07368E47F9140605CBBF

meeting-asr-subtitle_v3-before-zh_en_20261106-1017.zip
  D21C547FFE356146AA13619C9D0E6359D05035942BB658EAD66B2667E77AD590

meeting-asr-subtitle_v5-zh_en_20261106-1052.zip
  B4526A32E63EA16FE8C992ED49C350884544D6B17B3B92CAE922FEC7612F0595

meeting-asr-subtitle_v6-before-mute_20261106-1106.zip
  B1A6969BCAAC9B9178F1A96D61EDE0FF93792CEC37170D457B8A7F7272287800

meeting-asr-subtitle_v6-mute-gate_20261106-1233.zip
  887438205CA9D99513AC3215EE7FC09869955E88EA1236D874B458877BE79813

meeting-asr-subtitle_v7-before-failopen_20261106-1247.zip
  947DD2A2E638D52A61A2A6899293740916A6272EDC50A0C0B8D0920C33A2FC07

meeting-asr-subtitle_v6.1-docs-portable_20261106-1311.zip
  29199B366D2195994C1BD6F46BC4FCE309E8A046EB618C7035C4446FBE0E8F50

meeting-asr-subtitle_v7-before-selfid_20261106-1344.zip
  01F5BDBE60E77EB139A20E217CB1D46A910C0A42CE7FA1B172E603A850181606

meeting-asr-subtitle_v7-selfid-failopen_20261107-1133.zip
  8D0DCBAA455DF66376DEFA9A5065E5AECC94B2AB583AA640B5D13D973A8ADF12

meeting-asr-subtitle_v8-before-meeting-stop_20261107-1145.zip
  ED3B7117569540304A973E42456BF050793D0EEA32964BC106759E41D3ACD97E

meeting-asr-subtitle_v8-meeting-auto-stop_20261107-1202.zip
  6406EE2F618B3401DE632FB792C7483BB3CC9F8DD88530F300DD353BE6314A79

meeting-asr-subtitle_v8.1-window-filter_20261107-1240.zip
  B2BCCA8C16331774319CACF0EBEEE85C3E72771D13FBB4B4B1DB48FAC73D16BA

meeting-asr-subtitle_v8.2-docs-sync_20261107-1526.zip
  056E85FD5C2D040759C9B73D0CE5DD9B77D004CFBC4F368D91F1796E648A5E66

meeting-asr-subtitle_v8.3-before-mic-display_20261113-1318.zip
  3B8CD598B7F481DCDDC9B5EC828623918C61CCC66527490C631BECA83AFDCAD0

meeting-asr-subtitle_v8.3-mic-display_20261113-1347.zip
  A0DC0FC5761C3D1A565558FF9DC5C190CFB965D070361F22AD521702DD8F84E5

meeting-asr-subtitle_v8.4-before-mic-display-fix_20261113-1535.zip
  A40FF3F6C37822112EF9DA220543613AE40040CD833E67CA33F72A581BD57F6B

meeting-asr-subtitle_v8.4-mic-display-fix_20261113-1630.zip
  50F8CF74335743126771A63E083208A29A2EB5DE3E6A3CE461C859F83A2B7099

meeting-asr-subtitle_v8.5-before-auto-mic-switch_20261113-1720.zip
  7E55D7C99EEF53103356540C3E70FBE4638C12EFC9726453B34E55F6684B0BB6

meeting-asr-subtitle_v8.5-auto-mic-switch_20261113-1812.zip
  8A4E2D3B66FD870EC4A1BBB108BCEF2C859974D0B2DEE488802AE6C2A0527036

meeting-asr-subtitle_v8.6-before-no-cache_20261113-1841.zip
  EE588581A2F37E15B98EFE533B7A1FE91BB9AC0733755A90FF9CEC8C9B81B18A

meeting-asr-subtitle_v8.7-before-follow-default_20261113-1859.zip
  C678458C8A04D999097F0546E8D64BE1EF72CFA60325EC573B9C32DE853746EE

meeting-asr-subtitle_v8.7.1-before-name-device_20261113-1924.zip
  C978558ED734F2972DAAF3D4EABAC63CD62EB9941DDD230FDF2360A6903E20A3

meeting-asr-subtitle_v8.7-follow-default_20261113-1948.zip
  D6406F6CDE3819D81AD35DC543A0F5E0DAC1C64A7ECBBEAFA5D8A9EDAF93DCC1

便携包（含模型）meeting-asr-subtitle_portable_v6_20261106.zip
  aa4e10213c77dac6761aa875a36c966dda30b741ccf6c8c2a8e29884bec075ae

便携包（含模型）meeting-asr-subtitle_portable_v8.1_20261107.zip
  SHA256 见同名 .sha256 文件（包外校验，避免自引用）
```
