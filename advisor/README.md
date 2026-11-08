# advisor

负责本地会议会话：消费字幕、构造会议上下文、调用模型、校验并保存决策和本地结果，包含本地 LLM harness。所需客户端、配置、存储及会话实现由模块独立维护，不导入 meeting-listener。

飞书字幕链路已停用（见 [plan.md](../plan.md)）：advisor 不再要求 bot 入会，也不再向飞书发送会中消息或单聊通知；notification 搁置，模型回答保存为本地结果。

## 安装与配置

以下命令在 `advisor/` 执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

仅在没有本机配置时复制 `config.example.yaml` 为 `config.yaml`，填写凭据并执行 `chmod 600 config.yaml`。`storage.directory` 默认配置为 `../data`。

配置 `llm.backend: codex` 或 `opencode` 使用真实模型（本地子进程执行；远程 `llm-gateway` 按计划在阶段 3–4 接入，见 [远程推理契约](../docs/CONTRACT_REMOTE_INFERENCE.md)）。Codex 使用本机 ChatGPT 订阅登录；OpenCode 默认使用已登录的 `zai-coding-plan/glm-5.3`。`llm.role_file` 留空时使用包内 [meeting_adviser.md](src/feishu_meeting_adviser/meeting_adviser.md)，也可指定本机文件。

## 本地会话

```bash
.venv/bin/python -m feishu_meeting_adviser session-new --title 周会 [--description-file meeting.md]
.venv/bin/python -m feishu_meeting_adviser session-list
.venv/bin/python -m feishu_meeting_adviser consume --watch <session_id>
.venv/bin/python -m feishu_meeting_adviser status <session_id>
.venv/bin/python -m feishu_meeting_adviser replies <session_id> --show-text
.venv/bin/python -m feishu_meeting_adviser resolve-reply <session_id> --uuid <UUID> --outcome sent
.venv/bin/python -m feishu_meeting_adviser session-end <session_id> [--force]
```

- 会话显式创建，拥有独立时间轴与按会话命名的消费租约；同一会话同时只允许一个消费者，不同会话可并行。
- `consume` 完成输入、模型调用、证据复核并把回答保存到本地 outbox（`channel=local`，`replies` 查看）；不发生任何网络发送。
- `session-end` 在空闲时立即结束会话；有未决工作时置为 draining，消费循环在输入、任务与回复落定后自动结束。ended 会话不能再次消费。
- 创建会话时的 `--description-file` 是本会话唯一的会议材料来源（无飞书日历访问）；省略即禁用材料。
- 运行期字幕输入（fake-recorder → [本地交接契约](../docs/CONTRACT_RECORDER_INPUT.md)）在阶段 2 接入；当前字幕经由本地字幕存储进入会话。

## 日程材料和 harness（旧会话路径）

飞书日历材料与用户 OAuth 保留给旧数据与 harness 使用：

```bash
.venv/bin/python -m feishu_meeting_adviser.oauth login
.venv/bin/python -m meeting_adviser_harness \
  --description-file examples/description.md \
  --role-file src/feishu_meeting_adviser/meeting_adviser.md \
  --backend opencode
```

harness 使用本地描述、多人字幕和终端输出替换外部边界，不入会、不发送飞书消息。

## 开发与文档

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

- [OpenCode 后端](docs/OPENCODE_GLM.md)、[模型诊断](docs/MODEL_DIAGNOSTICS.md)
- [用户 OAuth](docs/USER_OAUTH.md)、[材料探测](docs/CONTEXT_PROBE.md)
- [回答发送渠道](docs/NOTIFICATIONS.md)（旧会话）、[harness](docs/HARNESS.md)
- [TODO](TODO.md)、[数据操作](../data/docs/DATA_OPERATIONS.md)、[联合运行](../README.md)、[plan.md](../plan.md)
