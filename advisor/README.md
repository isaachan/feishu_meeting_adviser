# advisor

负责消费字幕、读取日程材料、调用模型、保存决策和发送回答，包含本地 LLM harness。所需客户端、配置、存储及会话实现由模块独立维护，不导入 meeting-listener。

## 安装与配置

以下命令在 `advisor/` 执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

仅在没有本机配置时复制 `config.example.yaml` 为 `config.yaml`，填写凭据并执行 `chmod 600 config.yaml`。本模块独立维护配置；`storage.directory` 默认配置为 `../data`，与 meeting-listener 指向同一数据目录并使用相同应用身份。

配置 `llm.backend: codex` 或 `opencode` 使用真实模型；省略时保留固定测试规则。Codex 使用本机 ChatGPT 订阅登录；OpenCode 默认使用已登录的 `zai-coding-plan/glm-5.3`。`llm.role_file` 留空时使用包内 [meeting_adviser.md](src/feishu_meeting_adviser/meeting_adviser.md)，也可指定本机文件。

## 消费与回复管理

先由 meeting-listener 入会并采集，再运行：

```bash
./consume
# 单次或显式指定后端
.venv/bin/python -m feishu_meeting_adviser consume --backend codex
.venv/bin/python -m feishu_meeting_adviser consume --backend opencode --watch
.venv/bin/python -m feishu_meeting_adviser status --consumer adviser
.venv/bin/python -m feishu_meeting_adviser replies
.venv/bin/python -m feishu_meeting_adviser resolve-reply --uuid <UUID> --outcome sent
```

脚本使用本模块的 `.venv` 和 `config.yaml`。`consume` 持续保存输入并自主决定发言或沉默，回答一次完整发送；停止消费不影响采集或离会。同一数据目录只运行一个模型消费者。不确定发送需先核对会议界面，再使用 `sent`、`retry` 或 `cancel` 明确结果。

模型上下文预算、证据窗口、回复间隔及材料读取均由本模块配置。材料不会自行触发回答；配置、角色或日程材料更新后重启消费。通知目的地由 `notifications.meeting_comment` 和 `notifications.recipients` 控制，接收人填写当前应用的 `open_id`。

## 日程材料和 harness

```bash
.venv/bin/python -m feishu_meeting_adviser.oauth login
.venv/bin/python -m meeting_adviser_harness \
  --description-file examples/description.md \
  --role-file src/feishu_meeting_adviser/meeting_adviser.md \
  --backend opencode
```

`meeting_context.calendar_id` 一次绑定后，consume 按已保存会议号、入会时间定位唯一日程，读取描述及关联 Docx / Wiki 文档。harness 使用本地描述、多人字幕和终端输出替换外部边界，不入会、不发送飞书消息。

## 开发与文档

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

- [OpenCode 后端](docs/OPENCODE_GLM.md)、[模型诊断](docs/MODEL_DIAGNOSTICS.md)
- [用户 OAuth](docs/USER_OAUTH.md)、[材料探测](docs/CONTEXT_PROBE.md)
- [回答发送渠道](docs/NOTIFICATIONS.md)、[harness](docs/HARNESS.md)
- [TODO](TODO.md)、[数据操作](../data/docs/DATA_OPERATIONS.md)、[联合运行](../README.md)
