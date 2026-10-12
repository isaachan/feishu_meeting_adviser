# v2 阶段 0：应用机器人入会、事件探测与字幕查看

[计划](PLAN_OFFICIAL_MEETING_EVENTS.md) · [TODO](docs/TODO.md)

依据飞书[加入会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/join.md)及[会议事件列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events.md)接口：用应用身份执行 `bots/join`（输入 9 位会议号），**从入会响应直接获取内部会议 ID**，然后用相同应用身份拉事件。无需用户 OAuth，也不再使用 `list_by_no` 查 ID。接口处于灰度开放阶段；用户已在真实会议中观察到 `transcript_received`，并确认 `transcripts` 在本机打印出字幕正文；持续增量与端到端时延尚未验证。

## 准备

1. 创建并安装企业自建应用、配置应用机器人；在开发者后台申请/授予应用身份 **`vc:meeting.bot.join:write`**（入离会；事件接口对应用身份也推荐此权限）。参会者知情；主持人在当前会议中打开「AI 总结」，并在会议「安全」中打开「允许智能体加入会议」。开关不可见时先确认灰度与组织/客户端条件。
2. 在 `v2/` 安装依赖：
   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   # 仅当本机没有 config.yaml 时：
   cp config.example.yaml config.yaml
   chmod 600 config.yaml
   ```
3. **只在本机 `v2/config.yaml`** 填 `app.id`、`app.secret`、`lookup.meeting_no: "123456789"`（替换成实际 9 位会议号，保留引号）；如有入会密码，填 `join.password`。已有配置**不要覆盖**；旧 `oauth`、`probe.meeting_id` 可以保留但不会使用。脚本不从环境变量或命令行读取凭据/会议号，不改写 `config.yaml`。

## 操作

在 `v2/` 中执行：

```bash
.venv/bin/python scripts/stage0.py join
.venv/bin/python scripts/stage0.py probe
.venv/bin/python scripts/stage0.py transcripts  # 明确请求在本地终端显示真实字幕
# 仅在接口失败时，在本机检查错误原因（不要转发原始输出）：
.venv/bin/python scripts/stage0.py diagnose
```

- `join` 取得 `tenant_access_token`，向 `POST /open-apis/vc/v1/bots/join` 发送 `join_type=1`、`join_identify.meeting_no` 及可选密码。**入会成功且响应会议号匹配后**，把返回的长数字 ID 写入本地 `v2/session.yaml`（权限 `600`，Git 忽略）；不在终端打印会议 ID。新会议必须重新运行 `join`；不要对已入会机器人反复执行，以免重复入会失败。失败会清除旧 session，防止误用旧 ID。
- `probe` 从 `session.yaml` 读取入会返回的 ID；先验证它对应当前 `config.yaml` 的会议号，再请求 `GET /open-apis/vc/v1/bots/events`。不自动再次调用 `join`，单次读取最多 `probe.max_pages` 页（1–10）；只输出事件类型计数和字幕字段**类型**，不打印字幕正文/说话人/token/会议 ID/游标。
- **`transcripts` 会在本机标准输出逐句打印字幕正文（纯文本，每句一行）**，请只在获同意的会议中运行；不要重定向到公开日志、复制到聊天或提交仓库。脚本解析真实探测中观察到的顶层 `event_type=transcript_received`，也兼容文档示例的 `event_type=meeting.activity` + `payload.activity_event_type=transcript_received`；支持 `payload.transcript_received_items` 或直接包含 `text` 的单句 payload，`payload` 为 JSON 字符串或对象均可。单次调用内按 `sentence_id` 去重。没有字幕时会输出**仅含计数**的诊断信息（`events`、`transcript_events`、`transcript_items`、`event_types`、`activity_types`、`event_shapes`、`time_filter_active`；`event_types` 仅显示短的 ASCII 枚举样式值，`event_shapes` 仅显示固定字段名和类型，绝不显示字幕/参会人内容），不输出会议内容，便于区分无事件、无转写事件、空字幕项或误设时间过滤；页数超过上限或字段异常时拒绝输出不完整字幕。历史事件较多时可在本地 `config.yaml` 的 `probe` 下加 `start_time: "<10位Unix秒>"`，必要时加 `end_time` 缩小范围；`probe.max_pages` 最多 10。此命令**只读取一次**，重复运行可能打印以前的句子，不代表已实现实时增量轮询。
- `diagnose` 重发与 `probe` 相同的事件请求，在本机显示请求形状（GET、路径、查询参数**名称**、会议 ID **位数**、是否有游标，不显示 token/ID 值）。**仅在失败时**额外显示飞书 `msg`（替换已知 token/会议 ID/游标/应用凭据）及响应中可用的 `request_id`/`log_id`；服务端 `msg` 仍可能含其他敏感信息，**只在本机阅读，绝不原样粘贴到聊天/工单或保存公共日志**。普通 `probe`/`transcripts` 仍隐藏 `msg`。通过此诊断判断实际请求与[官方 events 接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events.md)（GET，`meeting_id` 必填，时间范围/游标可选）是否一致。
- 若 `join` 返回 `121016`，请先核对「AI 总结」「允许智能体加入会议」及灰度条件；`121003` 检查入离会权限；`121009/121010` 检查会议密码。若 `probe` 返回 `120004`，机器人并未成功入会或所用 ID 不对；其他错误结合官方文档确认。不要把真实凭据、原始响应或会议内容提交仓库/发送到聊天。

旧的 `scripts/lookup_meeting_id.py` 仅保留作独立诊断工具，它使用会议列表接口并需要**额外权限**；**正常入会→探测流程不需要它**。结束探测后请在飞书会议界面/受控流程中结束机器人参与；后续再接入官方 `bots/leave`，当前脚本没有自动离会功能。自动化测试（只用模拟响应，不访问飞书）：`.venv/bin/python -m unittest discover -s tests -v`。
