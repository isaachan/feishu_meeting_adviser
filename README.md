# 飞书实时会议 AI 助理

项目分为两个独立模块，通过本机 SQLite 数据交接：

- **[meeting-listener](meeting-listener/README.md)**：机器人入会、官方字幕采集、本地录制转写及字幕数据维护。
- **[advisor](advisor/README.md)**：消费字幕、读取日程材料、调用模型并发送回答，包含本地 harness。
- **[data](data/README.md)**：入会会话、字幕、消费进度、模型任务及运行日志。

两个模块分别维护配置、虚拟环境、测试和 `pyproject.toml`，所需重复实现各自保留。根目录 `integrations/` 默认全部是集成测试。

## 安装

从仓库根目录执行，要求 Python 3.11+：

```bash
python3 -m venv meeting-listener/.venv
meeting-listener/.venv/bin/python -m pip install -e './meeting-listener[stt]'
python3 -m venv advisor/.venv
advisor/.venv/bin/python -m pip install -e ./advisor
```

仅在模块还没有本机 `config.yaml` 时，复制各自的 `config.example.yaml` 并填写应用凭据，设置文件权限为 `600`。两套配置都将 `storage.directory` 设为 `../data`，并使用相同的应用身份。模型、日程及通知设置由 advisor 配置；meeting-listener 的 `llm.backend`、`llm.introduction`、`notifications.meeting_comment` 决定是否在入会时发送介绍。

## 运行

```bash
./meeting-listener/join <会议号或入会链接>
./meeting-listener/collect
# 另开一个终端
./advisor/consume
```

这些脚本使用所属模块的 `.venv` 和 `config.yaml`，可通过绝对路径从其他目录调用。`join` 脚本先按既有测试流程重置旧会话（没有旧会话时跳过），再入会。直接使用模块 CLI 的 `join` 命令则按会话状态核对和复用入会身份。

停止 `consume` 不会停止采集；停止 `collect` 不会离会。完整停止时先停止消费和采集，再在 `meeting-listener/` 执行 `.venv/bin/python -m meeting_listener leave`。

本地录制转写使用 `./meeting-listener/collect-local /path/to/growing.vemedia`，STT 代码、依赖和模型均由 meeting-listener 维护。

## 文档与测试

- 各模块的运行说明、单模块文档和测试见各自目录。
- 跨模块规划见 [总体计划](docs/PLAN_OFFICIAL_MEETING_EVENTS.md) 和 [TODO](TODO.md)。
- [飞书权限清单](docs/APP_PERMISSIONS_EVENTS.md)、[架构评审](docs/2026-10-15-architecture-review.md)、[实现记录](docs/IMPLEMENTATION_RECORD.md)。
- [数据操作](data/docs/DATA_OPERATIONS.md)说明保存、清理、授权与恢复边界。
- [集成测试说明](integrations/README.md)。

产品目标是在会议进行时获取内容并提供辅助。当前规划目标为提问结束到可用回答 20 秒；[字幕延迟调研](meeting-listener/docs/TRANSCRIPT_LATENCY_INVESTIGATION.md)显示拉取通道仍存在约 30 秒暴露延迟，20 秒是预算而非已实现的 SLA。跨会议可靠性和真实问答性能仍需验收。

独立可行性研究位于 [studies/local-meeting](studies/local-meeting/README.md) 和 [studies/listener](studies/listener/README.md)；官方接口实验位于 `meeting-listener/reference/spike/`。
