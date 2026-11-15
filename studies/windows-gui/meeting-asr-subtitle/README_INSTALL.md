# 解压即用说明（Windows）

> 本包包含代码、文档和运行所需的三个模型，不包含 Python 运行时和依赖库。
> 目标机器首次使用需要安装一次 Python 依赖，之后可直接双击 `start.bat`。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 2004+ / Windows 11 |
| Python | 3.11 ~ 3.13（推荐 3.13） |
| 内存 | 建议 8GB 以上（模型常驻约 500MB） |
| 磁盘 | 解压后约 300MB，另需给日志/缓存留空间 |
| 网络 | 首次 `pip install` 需要联网；运行识别时**不需要网络** |

---

## 2. 首次安装（只做一次）

### 2.1 安装 Python

从 <https://www.python.org/downloads/windows/> 安装 Python 3.13，安装时勾选
**Add python.exe to PATH**。

验证：

```bat
python --version
```

### 2.2 安装依赖

双击 **`setup.bat`**（或在命令行执行）：

```bat
setup.bat
```

它会执行：

```bat
python -m pip install -r requirements.txt
```

依赖包括：`sherpa-onnx`、`sounddevice`、`soundcard`、`process-audio-capture`、
`uiautomation`、`fastapi`、`uvicorn`、`numpy`、`websockets`。

如果公司网络需要代理，请先设置：

```bat
set HTTP_PROXY=http://proxy:port
set HTTPS_PROXY=http://proxy:port
setup.bat
```

---

## 3. 启动与使用

1. 双击 **`start.bat`**
2. 浏览器打开 <http://127.0.0.1:8066>
3. 选择使用方式：

| 场景 | 操作 |
|---|---|
| 飞书会议（推荐） | 点 **「飞书进程」**：远端=飞书进程音频，本地=麦克风，自动跟随飞书静音 |
| 抓整个系统声音 | 设备选「🔊 系统输出回环」，点「开始识别」 |
| 抓指定声卡/麦克风 | 设备选对应输入，点「开始识别」 |
| 会后转写录音 | 打开「参数」面板 → 选择 WAV → 「上传识别」 |

4. 停止：点页面「停止」，或双击 **`stop.bat`**
5. 页面底部会显示两个麦克风信息：
   - **麦克风**：本地通道实际绑定的设备，断开时显示「（已断开）」，停止时显示「（已停止）」
   - **默认麦**：Windows 当前默认输入；与绑定设备不一致时会标黄提示
6. 页面底部还会显示当前版本（如 `v8.7`）；`index.html` 已加 no-cache，更新后普通刷新即可

---

## 4. 使用飞书进程模式的注意事项

1. 飞书会议窗口需要**保持可见**（不能最小化到任务栏），否则静音检测读不到状态；
   窗口消失超过 10 秒会自动停止本地麦克风通道
2. 静音检测按 **Windows 显示名** 匹配飞书参会人列表里的自己；如果显示名和飞书昵称不一致，会显示「飞书麦：未知（放行）」，本地通道继续工作
3. 静音检测失败时默认**放行**本地麦克风，不会误停；可用参数「强制本地开麦」显式固定
4. 不支持按住空格临时发言（PTT），只识别点击静音/取消静音
5. 外放时麦克风会收进远端声音，建议戴耳机
6. 系统总音量拉到 0 仍可抓取飞书进程音频；在飞书里静音扬声器则抓不到
7. 浏览器标签页（标题里含「飞书会议」）不会被误判为会议窗口
8. 自动跟随 Windows 默认麦克风：插入 USB/蓝牙、手动改默认、拔掉旧设备，默认变化
   稳定 2 秒后都会自动切换本地通道并恢复字幕；没有默认麦克风时停止本地通道，
   远端字幕继续

---

## 5. 目录结构

```
meeting-asr-subtitle/
├── start.bat / stop.bat / setup.bat
├── main.py
├── asr_engine.py
├── audio_capture.py
├── process_loopback.py
├── feishu_mute.py
├── static/index.html
├── requirements.txt
├── API.md / PARAMS.md / ARCHITECTURE.md / CHANGELOG.md / README_INSTALL.md
├── backup.ps1
└── models/
    ├── sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/
    ├── 3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
    └── silero_vad.onnx
```

**不要移动或重命名 `models/` 里的文件**，代码按固定路径加载。

---

## 6. 常见问题

**端口被占用 / 页面打不开**

```bat
netstat -ano | findstr :8066
```

结束占用进程，或修改 `main.py` 里的 `PORT` / 启动参数 `--port`。

**提示没有找到麦克风**

检查 Windows「设置 → 隐私 → 麦克风」是否允许桌面应用访问；确认默认输入设备正确。
本机麦克风可以在「声音设置」里测试电平。

**进程环回不可用**

需要 Win10 2004+ / Win11；部分域控电脑可能被组策略禁用
`AUDIOCLIENT_PROCESS_LOOPBACK`。页面会给出明确错误。

**飞书麦状态一直「未知（放行）」**

- 这是安全行为：读不到状态时不会暂停本地麦克风
- 确认飞书会议窗口可见（不要最小化）
- 确认当前 Windows 显示名与飞书昵称一致（设置 → 账户）
- 需要严格固定本地通道时，打开参数「强制本地开麦」；想完全关闭跟随则关闭「跟随飞书静音」

**会议结束后本地还在收音**

- 正常应在会议窗口消失 10 秒后自动停止本地通道
- 如果仍持续，检查是否还有标题含「飞书会议」的飞书进程窗口
- 浏览器标签页不会被当作会议窗口（v8.1 起）

**首次启动很慢**

模型加载约 4~6 秒，属正常现象；之后常驻内存。

**换电脑后 Python 路径不同**

`start.bat` 会先找 `C:\Program Files\Python313\python.exe`，找不到时自动
使用 PATH 里的 `python`。如果都不对，编辑 `start.bat` 里的 `PY=` 一行。
