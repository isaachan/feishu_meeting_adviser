# meeting-listener

负责飞书机器人入会、离会、会话恢复，以及官方字幕轮询和本地 JSONL 字幕采集。

在 `v2/` 安装和运行：

```bash
python3 -m pip install -e ./meeting-listener
meeting-listener --config ./config.yaml join 012345678
meeting-listener --config ./config.yaml collect
meeting-listener --config ./config.yaml status
meeting-listener --config ./config.yaml leave
```

也可用 `python3 -m meeting_listener` 调用相同命令。数据维护命令包括 `reset-session`、`conflicts`、`resolve-conflict`、`prune` 和 `delete-meeting-data`。本地录制转写入口为 `v2/collect-local`，转发脚本位于 `scripts/collect-local.py`。

模块自行维护所需的配置、存储、会话与清理实现，不导入 advisor。配置结构、数据库格式、锁与既有行为保持一致；配置启用入会介绍时，`join` 仍发送介绍并记录其状态。

与 advisor 配置相同的应用和 `storage.directory`，即可通过本机 SQLite 数据交接。两个模块可安装在同一环境，也可使用各自的环境；从不同工作目录运行时，使用同一配置文件的绝对路径，避免默认 `config.yaml` 指向不同文件。配置和操作详情见 [工作区说明](../README.md)。
