# advisor

负责消费已采集字幕、读取日程材料、调用模型、保存决策和发送回答，包含本地 LLM harness。

在 `v2/` 安装和运行：

```bash
python3 -m pip install -e ./advisor
advisor --config ./config.yaml consume --backend codex --watch
advisor --config ./config.yaml status --consumer adviser
advisor --config ./config.yaml replies
advisor --config ./config.yaml resolve-reply --uuid <UUID> --outcome sent
```

Python 入口为 `python3 -m feishu_meeting_adviser`；材料授权和探测仍通过 `feishu_meeting_adviser.oauth`、`feishu_meeting_adviser.context_probe` 运行。harness 入口为 `python3 -m meeting_adviser_harness`。

模块自行维护所需的配置、存储、会话和客户端实现，不导入 meeting-listener。与采集端使用相同的应用和 `storage.directory`，通过本机 SQLite 读取字幕、记录消费进度并协调锁。可在独立环境中安装；从不同工作目录运行时，使用同一配置文件的绝对路径。

启动前由 meeting-listener 完成入会和采集。停止 `consume` 不会停止采集或离会。完整配置、恢复与模型后端说明见 [工作区说明](../README.md)。
