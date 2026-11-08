# 本机运行数据

`advisor/config.yaml` 和 `meeting-listener/config.yaml` 分别维护配置，两边的 `storage.directory` 默认均为 `../data`。两个模块必须使用相同的飞书应用和同一个本机数据目录，才能交接入会会话、字幕及消费进度。

| 路径 | 用途 |
|---|---|
| `source.sqlite3` | 入会会话、字幕、采集检查点、消费进度 |
| `adviser.sqlite3` | 模型上下文、任务、入会介绍和回复状态 |
| `locks/` | 采集、消费和维护操作的进程锁 |
| `diagnostics/` | 采集和模型诊断 |
| `fake_consumers/` | 固定测试规则的消费者状态 |
| `content_previews/`、`context_probe/` | 日程材料预览和探测结果 |
| `oauth/` | advisor 的用户授权状态 |
| `model_runtime/` | advisor 的模型临时运行资源 |
| `harness/` | advisor harness 的独立数据 |
| `llm-gateway/` | 网关任务数据库与隔离运行目录（见 [llm-gateway](../llm-gateway/README.md)） |
| `spike/` | meeting-listener 接口实验的独立配置、会话和输出 |

本目录不是 Python 包，不包含公共代码。实际运行数据由 Git 忽略，仅跟踪本说明及 [资料操作说明](docs/DATA_OPERATIONS.md)。STT 模型保存在 `meeting-listener/models/`。
