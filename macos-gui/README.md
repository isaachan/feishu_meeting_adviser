# macos-gui（会参谋采集客户端）

recorder 的图形分发形态：QR 扫码登录 → 会话列表自动发现 → 一键开始采集。设计依据：[DESIGN.md](DESIGN.md)；云端契约：CONTRACT_ADVISOR_API §8、CONTRACT_AUTH v1.2。

## 安装（命令行形态）

```sh
cd macos-gui
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'   # 含 PySide6 + segno（QR 渲染）
# 采集能力（G5+）需要 recorder 包：
.venv/bin/pip install -e ../recorder
```

注意：`recorder` 不在 pyproject 依赖里（模块独立纪律的显式例外，见 DESIGN §3）——`capture.py` 延迟导入，未安装时登录/列表功能不受影响，仅无法采集。

## 运行

```sh
.venv/bin/python -m macos_gui        # 或 .venv/bin/macos-gui
```

首次运行默认连接 `http://meeting-advisor.nioint.com`（免配置）→ 飞书扫码登录 → 之后每次启动直接进入会话列表。
在 Mac 上勾选“采集本机麦克风”后，采集会跟随飞书会议静音状态；请按系统提示授权
麦克风及屏幕录制权限。状态未知时不会打开本机麦克风。

## 本地凭据

- `device_token`（设备凭据，非用户 OAuth token）：macOS Keychain / Windows Credential Manager（经 keyring）；无后端环境自动退化为内存。
- 用户 OAuth token 永不出云端（红线，见 CONTRACT_AUTH §1）。

## 开发

```sh
.venv/bin/python -m unittest discover -s tests   # 26 项（核心 headless + Qt offscreen 冒烟）
.venv/bin/ruff check src tests
.venv/bin/pyright
```

- `config/api/login/sessions` 为无 Qt 核心逻辑，headless 可测。
- `app.py` + `ui/` 为 Qt 胶水；HTTP 调用在 worker 线程执行。
- 打包（.app / .exe）见 DESIGN §7，G6b 落地。
