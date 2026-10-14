# v2：产品级飞书字幕输入源

[计划](PLAN_OFFICIAL_MEETING_EVENTS.md) · [TODO](docs/TODO.md) · [Spike 实验与复现](reference/spike/README.md)

当前唯一路线是应用身份 `tenant_access_token` → `bots/join` → `bots/events`。接口可行性和单场连续采集已试通，证据见 [阶段 0 实测笔记](docs/STAGE0_FINDINGS.md)。阶段 1 的产品实现与自动化验证已完成至 1.8，100 个产品测试通过；真实会议回归和 G1 尚待完成，见 [验证记录](docs/STAGE1_VALIDATION.md)。历史代码和测试已隔离到 `reference/spike/`，产品不导入它们。已增加固定截止日期测试回复；模型问答尚未实现，阶段 2 将接入基于行为文档的 LLM 自主参与；20 秒 P95 是功能完成后的优化目标，不回退 v1。

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
.venv/bin/python -m feishu_meeting_adviser consume  # 持久化消费；匹配截止日期规则时发送会中回复，不调用 LLM
.venv/bin/python -m feishu_meeting_adviser leave
```

测试时可让终端 A 在采集过程中实时显示字幕：

```bash
.venv/bin/python -m feishu_meeting_adviser collect --show-text
```

每条新入库字幕立即输出一行 JSON，包含 `speaker`、`timestamp`、`text`，以及 `event_time`、`start_time`、`end_time`、`mode` 和 `kind`。`timestamp` 是本机接收时间（UTC），不是发言时间；其余时间保留上游原值，缺失为 `null`。`kind` 为 `added`（新增）、`updated`（已接受修订）或 `conflict`（待核对候选）。重复扫描不重复打印；重启后显示新入库内容，可能包括回补字幕。终端输出不改变消费进度，诊断日志仍隐藏正文。已有 `collect` 进程需先 Ctrl-C，再带开关重启，无需重新 join。

`consume` 增加一条固定测试规则：单条已接受字幕包含「项目截止日期是明天」（忽略空白）且有说话人姓名时，在当前已验证且仍为 joined 的会议发送「{实际说话人}，项目截止日期是今天。」。无需额外开关，不调用模型。没有姓名或未解决的冲突候选不回复；不跨字幕拼句。同一会议同一句字幕只回复一次，不同句子分别处理。这是关键词测试，不分析否定或引用语义。

每次 `consume` 仍处理一批待确认输入，包含尚未消费的首次回放/回补字幕；已确认的输入不会重新触发。输出中的 `replied` 是本次 API 已确认的发送数量，参会者实际看到消息仍需现场核对。回复正文和固定 UUID 在私有消费者数据库中与输入原子保存，API 确认后保存发送结果，再确认源输入。失败时保留该条及后续未确认记录，已完成的前缀正常确认；请求不会自动重试。超时或响应异常时先检查会议 UI，再按提示重试 `consume`，复用同一 UUID；服务端去重的有效范围仍待实测。当前需具备已试通的 `vc:meeting.message:write` 权限，接口依据见[会中输出](docs/MEETING_OUTPUT_RESEARCH.md)。

`collect` 重启自动恢复产品检查点与未确认输入；`collect --once` 只完成一个页数预算周期，仍可能有待追赶页。源和消费进度独立，同句无可靠版本的变化保留为冲突，已有上下文不自动覆盖；本机核对候选后可用 `resolve-conflict --delivery-id <候选交付哈希>` 显式选择。候选和正文保存于私有数据库中，终端默认只显示计数/状态，显式 `--show-text` 才显示内容。源码接口和时间规则见 [输入契约](docs/TRANSCRIPT_SOURCE_CONTRACT.md)，上游未知行为见 [接口笔记](docs/API_CONTRACT_NOTES.md)，日志、容量、保留和删除操作见 [资料管理](docs/DATA_OPERATIONS.md)。

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

`leave` 返回 `121104` 表示会议当前状态不支持离会，不能据此确认机器人已不在会中。若旧会议已经结束，或在会议界面确认机器人已离会，停止采集后执行 `reset-session --confirmed-bot-left`，再 `join` 新会议；重置只标记旧会话已离会，不删除字幕或消费进度。旧会议仍在进行且机器人仍在其中时，核对会议状态后重试 `leave`。

阶段 2 的已确认方向、待补边界和功能/性能验收顺序见[首版交互与验收口径](docs/STAGE2_INTERACTION.md)。不提供固定唤醒词或特殊控制指令，由 LLM 决定是否发言；会中 comment 文字一次完整发送。当前产品尚未实现这些模型能力。

阶段 2 模型入口（实现推进中，完整验收待 2.3）：

```bash
.venv/bin/python -m feishu_meeting_adviser consume --backend codex --watch
```

使用本机已登录的 Codex 订阅；不带 --watch 为单次处理。旧配置没有 llm 段时，原 consume 仍使用固定规则；配置 llm.backend: codex 后，consume 默认使用模型。固定规则也可显式 consume --backend rule。两个消费入口互斥使用，模型消费者默认为 adviser，与原 fake-qa 进度独立。
