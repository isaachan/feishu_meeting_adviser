# v2：产品级飞书字幕输入源

[计划](PLAN_OFFICIAL_MEETING_EVENTS.md) · [TODO](docs/TODO.md) · [Spike 实验与复现](reference/spike/README.md)

当前唯一路线是应用身份 `tenant_access_token` → `bots/join` → `bots/events`。接口可行性和单场连续采集已试通，证据见 [阶段 0 实测笔记](docs/STAGE0_FINDINGS.md)。阶段 1 的产品实现与自动化验证已完成至 1.8，65 个产品测试通过；真实会议回归和 G1 尚待完成，见 [验证记录](docs/STAGE1_VALIDATION.md)。历史代码和测试已隔离到 `reference/spike/`，产品不导入它们。真实问答尚未实现，20 秒端到端目标仍待阶段 2 验收，不回退 v1。

## 目录与安装

- `src/feishu_meeting_adviser/`：产品 Python 包。
- `tests/`：产品测试。
- `reference/spike/`：旧实验代码、测试、配置示例及复现说明。
- `docs/`：接口观察、计划与脱敏测试记录。

从 `v2/` 执行（Python 3.11+）：

```bash
python3 -m venv .venv  # 已有环境可直接使用
.venv/bin/python -m pip install -e .
.venv/bin/python -m feishu_meeting_adviser --help
.venv/bin/python -m unittest discover -s tests -v
```

默认读取工作目录的 `config.yaml`（以下命令从 `v2/` 执行），其他目录使用 `--config /绝对路径/config.yaml`。默认运行资料保存在配置文件同目录下的 `data/`。产品读取旧配置的 `app`、`lookup.meeting_no`、`join.password` 和 `probe.timeout_seconds`，不改写已有配置；旧 `stream`、`message` 和旧 session 不用于产品。新设置参考 `config.example.yaml`，仅在没有配置文件时复制并设为 `600`。真实资料、凭据、session 和运行数据均不提交仓库。

## 产品操作

真实入会前需参会者同意，当前会议需已具备阶段 0 验证的权限和设置。先在界面确认 spike 机器人已离会；不要在已有机器人占用的情况下盲目重复入会。

```bash
.venv/bin/python -m feishu_meeting_adviser join
.venv/bin/python -m feishu_meeting_adviser collect
# Ctrl-C / SIGTERM 仅停止采集，bot 仍在会中；不要在采集中调用 leave。
.venv/bin/python -m feishu_meeting_adviser status
.venv/bin/python -m feishu_meeting_adviser consume  # 持久化给假消费者后确认，不调用 LLM、不发送消息
.venv/bin/python -m feishu_meeting_adviser leave
```

`collect` 重启自动恢复产品检查点与未确认输入；`collect --once` 只完成一个页数预算周期，仍可能有待追赶页。源和消费进度独立，同句无可靠版本的变化保留为冲突，已有上下文不自动覆盖；本机核对候选后可用 `resolve-conflict --delivery-id <候选交付哈希>` 显式选择。候选和正文仅在私有数据库中，终端默认只显示计数/状态。源码接口和时间规则见 [输入契约](docs/TRANSCRIPT_SOURCE_CONTRACT.md)，上游未知行为见 [接口笔记](docs/API_CONTRACT_NOTES.md)，日志、容量、保留和删除操作见 [资料管理](docs/DATA_OPERATIONS.md)。

同一应用共用一个产品数据目录，目录内的采集与入离会操作互斥；重复 join/leave 会使用已确认的本地结果。切换会议前先停止采集、离开保存的旧会议，再修改会议号并 join。`leave` 始终针对保存的已验证会议 ID，配置改变不会让它猜测另一个 ID。配置在运行中变化时停止采集，需重新启动。

入离会超时会保留不确定状态，不能用本地 session 推断机器人已入会或离会。先在会议界面核对；离会仍需重试时显式使用 `leave --retry-uncertain`，只有已确认机器人离会后才能用 `reset-session --confirmed-bot-left` 清理不确定或实际已离会的过期 session。不会自动重新入会。权限撤销、机器人离会和无法识别的格式会报告需人工处理；空页不会被当作会议结束。

## Spike 复现

从 `v2/` 执行：

```bash
.venv/bin/python -m pip install -r reference/spike/requirements.txt
.venv/bin/python -m unittest discover -s reference/spike/tests -v
.venv/bin/python reference/spike/scripts/stage0.py --help
```

真实会议操作见 [Spike README](reference/spike/README.md)，仍从本机 `v2/config.yaml` 读取凭据。复现实验需参会者同意；迁移没有复制真实配置、session 或字幕。实验验证和产品验证分别记录。
