# 飞书实时会议 AI 助理

在 `v2/` 目录运行：

```bash
cd v2
./join
./collect
# 另开一个终端，同样先 cd v2
./consume
```

先在 `v2/` 运行 `python3 -m pip install -e ./meeting-listener -e ./advisor` 安装两个模块。这些测试脚本使用 `v2/.venv` 和 `v2/config.yaml`，可从其他工作目录通过绝对路径调用。`./join`、`./collect` 调用 meeting-listener，`./consume` 调用 advisor。`./join` 先执行 `reset-session --confirmed-bot-left`（没有旧会话时跳过），再入会；这会把旧会话直接标记为已离会。`./collect` 与 `./consume` 持续运行，按 Ctrl-C 停止。

项目目标是在会议进行时获取讨论内容并提供 AI 辅助；会后逐字稿不能代替会中输入。**当前产品主线为 v2 官方会中字幕路线。**独立可行性研究位于 `studies/`。

- **[v2](v2/README.md)**：产品方案，包含 [meeting-listener](v2/meeting-listener/README.md)（入会与采集）和 [advisor](v2/advisor/README.md)（消费、模型决策与回答）两个模块，通过本机 SQLite 交接。主线以应用身份获取官方会中字幕（轮询 `bots/events` 的 `transcript_received`）；本地转写输入由 `collect-local` 提供。接口试验代码在 `v2/reference/spike/`；跨会议回归及 20 秒目标仍待验收。不达标时记录问题并调整产品/技术方案。
- **[本地会议研究](studies/local-meeting/README.md)**：读取录制中的本地 `.vemedia` 文件、提取音频并用流式 STT 转写。缺少可靠的说话人信息，会议上下文有限；解析、解码和 STT 模块供 `v2/collect-local` 复用。

**原 6 秒目标已撤销。新的规划目标为 20 秒**：约 15 秒取得会中字幕，约 5 秒由 LLM 生成/输出回答；从提问结束到可用回答的精确计时终点及统计口径仍待确认。v2 调研（25 场会议，见 [延迟调研](v2/docs/TRANSCRIPT_LATENCY_INVESTIGATION.md)）显示字幕从接口结束时间到本机接收中位约 31 秒、P95 约 40 秒（早前单场曾测得约 15–24 秒，属场次波动），因此 20 秒**是预算，不是已实现的 SLA**，拉取通道下不可达，达标路径是 v2 接入事件推送。`studies/local-meeting/material/` 和 `v2/output/` 可能包含敏感会议资料；请按授权范围使用，不要将本地模型、虚拟环境或实验产物提交到仓库。
