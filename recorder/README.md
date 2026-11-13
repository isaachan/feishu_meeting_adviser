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
麦克风（可选）需在"隐私与安全性 → 麦克风"授权终端 App。

## 使用

粘贴 bot 下发的命令，形如：

```sh
~/.huicanmou-recorder/bin/recorder real --session 's-xxxx' --event 'cal_x/evt_y' \
  --url ws://<server>:8400/ingest --token 'st-xxxx' [--mic]
```

- 远端回路常开；`--mic` 仅在明确开麦时出现（隐私默认关）。
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
