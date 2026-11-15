# 本地会议字幕项目 — 交接文档

> 当前版本：**v8.7**（2026-11-13）
> 详细架构见 `ARCHITECTURE.md`，接口见 `API.md`，参数见 `PARAMS.md`，
> 部署见 `README_INSTALL.md`，版本历史见 `CHANGELOG.md`。

---

## 1. 项目定位

飞书会议旁路采集 → 本地离线识别 → Web 实时字幕。音频全程不上传。

- 识别：SenseVoice（非流式，VAD 断句后整句输出）
- 声纹：3dspeaker ERes2Net 512 维在线聚类
- VAD：Silero
- 采集：系统回环 / 指定声卡 / 飞书进程级环回

---

## 2. 三种使用模式

| 模式 | 入口 | 说明 |
|---|---|---|
| 设备模式 | 页面「开始识别」 | 选声卡或系统输出回环 |
| 飞书进程模式 | 页面「飞书进程」 | 远端=飞书进程音频；本地=麦克风；自动跟随飞书静音 |
| 上传模式 | 参数面板 → 上传识别 | 上传 WAV，走 `/api/transcribe` |

---

## 3. 运行

```powershell
python -m pip install -r requirements.txt
python main.py
# 或双击 start.bat
# 访问 http://127.0.0.1:8066
```

停止：页面「停止」或 `stop.bat`。

---

## 4. 关键文件

| 文件 | 作用 |
|---|---|
| `main.py` | FastAPI、REST/WS、会话管理、Hub 广播 |
| `asr_engine.py` | 模型加载、VAD、识别、声纹、参数、会话线程 |
| `audio_capture.py` | 设备枚举、系统回环、重采样、麦克风采集 |
| `process_loopback.py` | 飞书进程级环回（2.5s 分段） |
| `feishu_mute.py` | 多信号静音检测（纯 UIA） |
| `static/index.html` | 前端页面 |
| `models/` | SenseVoice + 3dspeaker + Silero VAD |

---

## 5. 当前行为要点

- 静音检测优先级：悬浮工具栏 → 参会人自己的行 → 视频小窗 → 小尺寸悬浮窗 →
  未知
- 状态未知：**放行**本地麦克风（避免误停）
- 会议窗口消失超过 **10 秒**：自动停止本地通道（释放麦克风），远端继续
- 前端对单通道停止只更新提示，不影响全局运行状态
- 浏览器标签页（标题含「飞书会议」）不会被误判为会议窗口
- `force_local_mic` 可强制本地开麦，优先级最高
- **跟随 Windows 默认麦克风**：默认变化稳定 2 秒后自动切换本地通道；
  插入 USB/蓝牙、手动改默认、拔掉旧设备都会触发；无默认则停止本地通道
- 切换按**设备名称**打开，避免热插拔后 PortAudio 索引失效
- 页面底部显示：版本、麦克风（绑定）、默认麦、飞书麦

---

## 6. 已知限制

1. 自己身份按 Windows 显示名匹配，可用 `FEISHU_SELF_NAME` 覆盖；页面选择器未做
2. 不支持空格 PTT 临时发言
3. 进程环回分段约 2% 音频损失
4. 当前 sherpa-onnx 的 SenseVoice 绑定不支持热词
5. 服务只监听 `127.0.0.1`，无鉴权，不要暴露公网
6. 自动切换每次会中断 1~2 秒本地音频
7. USB 接收器仍插着、只拔耳机时，Windows 不移除端点，不会触发切换
8. 只在 Windows 默认变化时跟随；飞书内部选麦不改系统默认时检测不到

---

## 7. 版本与备份

- 改动前：`backup.ps1 -Tag vN -Note "说明"`
- 记录：`CHANGELOG.md`（版本、备份文件、SHA256、回退方式）
- 便携包（含模型）：`meeting-asr-subtitle_portable_v8.7_20261113.zip`
