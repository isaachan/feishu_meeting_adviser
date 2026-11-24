# Windows 会参谋采集客户端

Windows GUI 与 `gui` 共用 PySide6 主体和 recorder 上行。支持 Windows 10 2004 及以上、Windows 11，需使用飞书桌面客户端。

## 构建 exe

在 **Windows x64** 安装 Python 3.12 x64 后，从仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\windows-gui\build.ps1
```

构建脚本建立独立虚拟环境、安装依赖并使用 PyInstaller。结果为 `windows-gui\dist\MeetingAdvisor\MeetingAdvisor-<yyyyMMdd-HHmmss>.exe`（时间戳默认取构建时刻，也可用 `-Stamp` 指定），可单独分发。程序启动时会自动将运行依赖解压到系统临时目录，启动可能需要一些时间。也可在 GitHub Actions 中手动运行 `Windows GUI build` workflow，下载产物。

## 使用

1. 启动 `MeetingAdvisor.exe`，首次输入服务器地址并扫码登录。
2. 在飞书给会参谋发送入会指令；在客户端列表中选择会话。
3. 如需采集本人发言，勾选「采集本机麦克风」，再开始采集；默认不勾选。
4. 界面显示飞书进程采集、mic 状态和上行计数。飞书静音、检测未知或检测失效时本机 mic 暂停；确认开麦后恢复。静音期间发送的是合成零值音频，不采集 mic 声音。
5. 点击「停止采集」后关闭窗口；Windows 采集中关闭窗口会提示先停止，避免窗口隐藏后无法恢复。

只采集飞书相关进程输出；不使用系统全局回环。参考实现的进程环回依赖分段文件，分段边界可能丢失少量音频。麦克风跟随 Windows 默认输入设备；若飞书内部指定了不同的设备，请将 Windows 默认输入与飞书设置对齐。状态检测依赖飞书桌面会议窗口的中文无障碍控件及其可见性；无法确认状态时 mic 保持暂停。

## 真机验收

在能访问会参谋服务的 Windows 机器上测试：扫码登录、发现会话、只采飞书声音；勾选 mic 后切换飞书静音/开麦，验证暂停和恢复；最小化/关闭会议窗口、设备热插拔与默认 mic 切换，确认状态提示和实际采集结果。检查本地保存目录 `%LOCALAPPDATA%\gui\recorder\audio` 中的音频及云端上行；长时间静音后确认恢复仍可上行。
