# gui（会参谋采集客户端）

recorder 的图形分发形态：QR 扫码登录 → 单会话连接监视器。界面以 [图 4](design/monitor-concept.png) 为准；云端契约：CONTRACT_ADVISOR_API §8、CONTRACT_AUTH v1.2。

## 安装（命令行形态）

```sh
cd gui
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'   # 含 PySide6 + segno（QR 渲染）
# 采集能力（G5+）需要 recorder 包：
.venv/bin/pip install -e ../recorder
```

注意：`recorder` 不在 pyproject 依赖里（模块独立纪律的显式例外，见 DESIGN §3）——`capture.py` 延迟导入，未安装时登录与主页展示不受影响，仅无法连接。

## 运行

```sh
.venv/bin/python -m gui        # 或 .venv/bin/gui
```

首次运行默认连接 `http://meeting-advisor.nioint.com`（免配置）→ 飞书扫码登录 → 之后直接进入单会话监视器。主界面无会话选择：灰色为无会话，红色为会话存在但连接断开，绿色“已入会”为连接正常。飞书静音时图标加斜杠，界面不显示音频采集文案。
设置中可修改服务器地址，并选择“发现会话后自动连接”（默认关闭）。手动模式显示“连接／断开”；自动模式隐藏这两个按钮。换服务器地址会清除设备凭据并要求重新扫码。自动连接每个会话只发起一次；暂停后不会自动重连。若服务返回多个会话，客户端拒绝任选一个连接。
最小化会收进系统托盘（macOS 为菜单栏图标），不占任务栏/程序坞。托盘图标颜色反映状态（灰/绿/红）；Windows 左键单击或双击恢复窗口，macOS 用菜单首项「打开会参谋」恢复，右键菜单提供连接/断开/设置/退出。首次收进托盘会提示一次。关闭窗口与退出仍沿用原语义：采集中 macOS 隐藏、Windows 拒绝关闭；空闲关闭即退出。
本机麦克风随连接固定启用，并跟随飞书会议静音状态；请按系统提示授权麦克风及屏幕录制权限。状态未知时不会打开本机麦克风。

## 打包

平台构建脚本与平台代码分离：`gui/macos/`、`gui/windows/` 只放构建脚本；共用的 PyInstaller 入口是 `gui/launcher.py`；平台差异（采集源、留存目录、关窗策略）集中在 `src/gui/platforms/`。

### macOS（.app）

在装有 Python 3.11+ 与 Xcode Command Line Tools（swiftc）的 Mac 上，从仓库任意位置运行：

```sh
sh gui/macos/build.sh
```

脚本建立独立虚拟环境、预编译 Swift helper（SCK 采集 + 静音检测）并运行 PyInstaller。结果为 `gui/dist/macos/MeetingAdvisor.app`（onedir，架构跟随构建机），以及可直接分发的 `gui/dist/macos/MeetingAdvisor-macos-<arch>.zip`（ditto 压缩，保留可执行位）。也可在 GitHub Actions 中手动运行 `macOS GUI build` workflow，下载产物。

不签名/不公证（v1 接受项，DESIGN §7）：首次运行请**右键打开**绕过 Gatekeeper 引导；TCC 授权（屏幕录制 = SCK 采集、麦克风 = 开 mic 时）一次授权长期有效。打包机无需在目标机安装任何依赖。

### Windows（.exe）

在 Windows x64 安装 Python 3.12 x64 后，从仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\gui\windows\build.ps1
```

结果为 `gui\dist\windows\MeetingAdvisor\MeetingAdvisor-<yyyyMMdd-HHmmss>.exe`（单文件，时间戳默认取构建时刻）。构建、使用与验收细节见 [windows/README.md](windows/README.md)。也可在 GitHub Actions 中手动运行 `Windows GUI build` workflow。

采集中关窗的平台差异：macOS 隐藏窗口继续采集；Windows 拒绝关闭并提示先断开（`src/gui/platforms/`）。

## 本地凭据

- `device_token`（设备凭据，非用户 OAuth token）：macOS Keychain / Windows Credential Manager（经 keyring）；无后端环境自动退化为内存。
- 用户 OAuth token 永不出云端（红线，见 CONTRACT_AUTH §1）。

## 开发

```sh
.venv/bin/python -m unittest discover -s tests   # 核心 headless + Qt offscreen 冒烟
.venv/bin/ruff check src tests
.venv/bin/pyright
```

- `config/api/login/sessions` 为无 Qt 核心逻辑，headless 可测。
- `app.py` + `ui/` 为 Qt 胶水；HTTP 调用在 worker 线程执行。
- 打包（.app / .exe）见 DESIGN §7，G6b 落地。
