# macos-gui（会参谋采集客户端）

recorder 的图形分发形态：QR 扫码登录 → 单会话连接监视器。界面以 [图 4](design/monitor-concept.png) 为准；云端契约：CONTRACT_ADVISOR_API §8、CONTRACT_AUTH v1.2。

## 安装（命令行形态）

```sh
cd macos-gui
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'   # 含 PySide6 + segno（QR 渲染）
# 采集能力（G5+）需要 recorder 包：
.venv/bin/pip install -e ../recorder
```

注意：`recorder` 不在 pyproject 依赖里（模块独立纪律的显式例外，见 DESIGN §3）——`capture.py` 延迟导入，未安装时登录与主页展示不受影响，仅无法连接。

## 运行

```sh
.venv/bin/python -m macos_gui        # 或 .venv/bin/macos-gui
```

首次运行默认连接 `http://meeting-advisor.nioint.com`（免配置）→ 飞书扫码登录 → 之后直接进入单会话监视器。主界面无会话选择：灰色为无会话，红色为会话存在但连接断开，绿色“已入会”为连接正常。飞书静音时图标加斜杠，界面不显示音频采集文案。
设置中可修改服务器地址，并选择“发现会话后自动连接”（默认关闭）。手动模式显示“连接／断开”；自动模式隐藏这两个按钮。换服务器地址会清除设备凭据并要求重新扫码。自动连接每个会话只发起一次；暂停后不会自动重连。若服务返回多个会话，客户端拒绝任选一个连接。
本机麦克风随连接固定启用，并跟随飞书会议静音状态；请按系统提示授权麦克风及屏幕录制权限。状态未知时不会打开本机麦克风。

## 打包（.app）

在装有 Python 3.11+ 与 Xcode Command Line Tools（swiftc）的 Mac 上，从仓库任意位置运行：

```sh
sh macos-gui/build.sh    # 或 ./macos-gui/build.sh
```

脚本建立独立虚拟环境、预编译 Swift helper（SCK 采集 + 静音检测）并运行 PyInstaller。结果为 `macos-gui/dist/MeetingAdvisor.app`（onedir，架构跟随构建机），以及可直接分发的 `dist/MeetingAdvisor-macos-<arch>.zip`（ditto 压缩，保留可执行位）。也可在 GitHub Actions 中手动运行 `macOS GUI build` workflow，下载产物。

不签名/不公证（v1 接受项，DESIGN §7）：首次运行请**右键打开**绕过 Gatekeeper 引导；TCC 授权（屏幕录制 = SCK 采集、麦克风 = 开 mic 时）一次授权长期有效。打包机无需在目标机安装任何依赖。

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
