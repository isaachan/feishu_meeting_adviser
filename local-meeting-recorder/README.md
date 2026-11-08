# local-meeting-recorder

在终端用户 laptop 上运行，把本地会议音频转为字幕并交接给 advisor。真实声卡采集与 ASR **暂缓**（见 [plan.md](../plan.md) 阶段 5）；当前提供 **fake 模式**：按预写台词及时间间隔回放，产出与真实采集完全一致的契约事件。

- 交接契约（事件格式、spool/ack、幂等与恢复）：[recorder 输入契约](../docs/CONTRACT_RECORDER_INPUT.md)
- 会话由 advisor 创建（`advisor session-new`），本模块只引用 `session_id`

## 安装

以下命令在 `local-meeting-recorder/` 执行（fake 模式仅用标准库）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## fake 回放

```bash
.venv/bin/recorder fake examples/standup.jsonl --session <session_id> \
  [--data-root ../data] [--speed 10]
```

- 台词为 JSONL：`{"speaker": "张三", "text": "……", "delay_ms": 1500}`；`speaker` 可为 null，`delay_ms` 是该句之前的间隔。
- 回放启动时刻即会话时间轴原点；`--speed` 等比缩短等待（时间戳仍按原间隔换算，见契约第 5 节）。
- 事件按契约写入 `<data-root>/recorder/spool/<session>/<stream>/`；advisor 消费后写回 ack 游标，本模块据此清理已确认段。
- 正常结束发送 `stream_end`；Ctrl-C 干净停止且**不**发送 `stream_end`（已写入事件保留，等待 advisor 确认）。
- `--data-root` 默认指向仓库 `data/`（开发用途）；其他位置显式传入。

## 开发与测试

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

跨模块链路验证（fake-recorder → advisor）在根 [integrations/](../integrations/README.md)。
