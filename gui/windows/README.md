# Windows 会参谋采集客户端

Windows GUI 与 `gui` 共用 PySide6 主体、recorder 上行和 `gui/launcher.py`。支持 Windows 10 2004 及以上、Windows 11，需使用飞书桌面客户端。

## 构建 exe

在 **Windows x64** 安装 Python 3.12 x64 后，从仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\gui\windows\build.ps1
```

构建脚本建立 `gui\.venv`、安装 `recorder[windows]` 与 `gui[ui]`，使用 PyInstaller 生成单文件 exe。结果为 `gui\dist\windows\MeetingAdvisor\MeetingAdvisor-<yyyyMMdd-HHmmss>.exe`（时间戳默认取构建时刻，也可用 `-Stamp` 指定），可单独分发。程序启动时会自动将运行依赖解压到系统临时目录，启动可能需要一些时间。也可在 GitHub Actions 中手动运行 `Windows GUI build` workflow，下载产物。

## 使用

1. 启动 `MeetingAdvisor.exe`；首次使用输入服务器地址并扫码登录。
2. 在飞书给会参谋发送入会指令；主界面只显示该用户唯一的活跃会话。
3. 手动模式点「连接」，或在设置中打开「发现会话后自动连接」；本机麦克风固定启用，无开关。
4. 主界面显示连接状态；飞书静音时麦克风图标加斜杠。静音、检测未知或检测失效时本机 mic 暂停；确认开麦后恢复。静音期间发送的是合成零值音频，不采集 mic 声音。
5. Windows 在连接中关闭窗口会提示「请先断开连接，再关闭窗口」，避免窗口隐藏后无法恢复；先点「断开」再关闭。最小化会把窗口收进通知区域（托盘），托盘图标按状态显示灰/绿/红，左键恢复窗口，右键菜单可连接、断开、设置或退出；首次收进托盘会提示一次。

只采集飞书相关进程输出；不使用系统全局回环。参考实现的进程环回依赖分段文件，分段边界可能丢失少量音频。麦克风跟随 Windows 默认输入设备；若飞书内部指定了不同的设备，请将 Windows 默认输入与飞书设置对齐。状态检测依赖飞书桌面会议窗口的中文无障碍控件及其可见性；无法确认状态时 mic 保持暂停。

## 真机验收

在能访问会参谋服务的 Windows 机器上测试：扫码登录、发现会话、只采飞书声音；切换飞书静音/开麦，验证暂停和恢复；最小化/关闭会议窗口、设备热插拔与默认 mic 切换，确认状态提示和实际采集结果。检查本地保存目录 `%LOCALAPPDATA%\gui\recorder\audio` 中的音频及云端上行；连接中关窗应被拒绝，先断开后可正常退出；长时间静音后确认恢复仍可上行。
