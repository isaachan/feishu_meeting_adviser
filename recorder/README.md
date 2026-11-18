# recorder（会参谋采集器）

laptop 端采集器（方案 C）：双路采集（SCK 环回 + 可选麦克风）、整场音频本地留存、
按 CONTRACT_RECORDER_STREAM v1 经 WS 上行到云端 advisor ingest。**零配置零凭据**——
只按 bot 下发的命令执行（附录 A 参数），不保存任何用户 OAuth 凭据。

## 安装（Mac）

前置：Python 3.11+、Xcode Command Line Tools（swiftc，用于编译 SCK 环回 helper）、
PortAudio（`brew install portaudio`，仅开麦时需要）。

```bash
# 1) 拿到安装包（whl，见下方"构建"）后：
python3 -m venv ~/.huicanmou-recorder
~/.huicanmou-recorder/bin/pip install --upgrade pip
~/.huicanmou-recorder/bin/pip install recorder-<version>-py3-none-any.whl

# 2) 自检（会现场编译 SCK helper，首次约几十秒）：
~/.huicanmou-recorder/bin/recorder --version
```

macOS Gatekeeper：whl 是纯 Python 包，不走公证流程；若 SCK helper 二进制被拦截，
在"系统设置 → 隐私与安全性"允许，或预先设置 `RECORDER_SCK_BIN` 指向受信二进制。
麦克风（可选）需在“隐私与安全性 → 麦克风”授权运行 recorder 的 App；跟随飞书静音
还需在“隐私与安全性 → 辅助功能”按首次运行提示授权检测程序。
未授权、未找到飞书会议窗口或状态无法确认时，本机麦克风保持关闭。

## 使用

粘贴 bot 下发的命令，形如：

```sh
~/.huicanmou-recorder/bin/recorder real --session 's-xxxx' --event 'cal_x/evt_y' \
  --url ws://<server>:8400/ingest --token 'st-xxxx' [--mic]
```

- 远端回路常开；`--mic` 仅在明确选择采集本机麦克风时出现（默认关）。
  Mac 上开启后仅在飞书会议麦克风状态确认“开麦”时采集；静音时释放输入设备。
- Ctrl-C 中断不会伪造干净收尾（缓冲保留待重连）；`--duration N` 到时干净停止。
- 退出码：0 干净；2 参数/令牌非法；3 连接不可恢复；130 Ctrl-C。
- 整场音频留存于 `--data-root（默认 ./data）/recorder/audio/<session>/`，手动清理。

回放模式（集成测试床）：`recorder replay script.jsonl --session … --event … --url … --token …`，
script.jsonl 每行 `{"delay_ms": 500}`。

## 构建（分发方）

```bash
cd recorder
python3 -m build --wheel          # 产物在 dist/（未签名/公证，README 如上引导绕过 Gatekeeper）
```

升级：重新 pip install 新 whl 即可；SCK helper 按源码哈希缓存，变更自动重编。

## 开发

```bash
pip install -e '.[dev]'
ruff check src tests && pyright
python -m unittest discover -s tests -v   # 约 48s（含重连退避计时）
```

Mac 飞书会议窗口若不暴露辅助功能按钮，可在会议中用以下诊断程序各采集一份
“静音”和“开麦”状态。它只保存麦克风按钮附近的小图，不保存整个会议窗口：

```sh
swiftc -O -parse-as-library tools/probe_macos_mute.swift -o /tmp/probe_macos_mute
# 在飞书会议中静音时：
/tmp/probe_macos_mute /tmp/feishu-mute-probe/muted
# 手动取消静音后：
/tmp/probe_macos_mute /tmp/feishu-mute-probe/unmuted
```
