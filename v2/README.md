# v2 阶段 0：应用机器人入会与事件探测

[计划](PLAN_OFFICIAL_MEETING_EVENTS.md) · [TODO](docs/TODO.md)

依据飞书[加入会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/join.md)及[会议事件列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events.md)接口：用应用身份执行 `bots/join`（输入 9 位会议号），**从入会响应直接获取内部会议 ID**，然后用相同应用身份拉事件。无需用户 OAuth，也不再使用 `list_by_no` 查 ID。接口处于灰度开放阶段，尚未在本项目真实会议中验证。

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
```

- `join` 取得 `tenant_access_token`，向 `POST /open-apis/vc/v1/bots/join` 发送 `join_type=1`、`join_identify.meeting_no` 及可选密码。**入会成功且响应会议号匹配后**，把返回的长数字 ID 写入本地 `v2/session.yaml`（权限 `600`，Git 忽略）；不在终端打印会议 ID。新会议必须重新运行 `join`；不要对已入会机器人反复执行，以免重复入会失败。失败会清除旧 session，防止误用旧 ID。
- `probe` 从 `session.yaml` 读取入会返回的 ID；先验证它对应当前 `config.yaml` 的会议号，再请求 `GET /open-apis/vc/v1/bots/events`。不自动再次调用 `join`，单次读取最多 `probe.max_pages` 页（1–10）；只输出事件类型计数和字幕字段**类型**，不打印字幕正文/说话人/token/会议 ID/游标。官方事件使用 `event_type=meeting.activity`，字幕子类型是 `payload.activity_event_type=transcript_received`。该工具不持续轮询，两次探测的计数不等于实时增量验收。
- 若 `join` 返回 `121016`，请先核对「AI 总结」「允许智能体加入会议」及灰度条件；`121003` 检查入离会权限；`121009/121010` 检查会议密码。若 `probe` 返回 `120004`，机器人并未成功入会或所用 ID 不对；其他错误结合官方文档确认。不要把真实凭据、原始响应或会议内容提交仓库/发送到聊天。

旧的 `scripts/lookup_meeting_id.py` 仅保留作独立诊断工具，它使用会议列表接口并需要**额外权限**；**正常入会→探测流程不需要它**。结束探测后请在飞书会议界面/受控流程中结束机器人参与；后续再接入官方 `bots/leave`，当前脚本没有自动离会功能。自动化测试（只用模拟响应，不访问飞书）：`.venv/bin/python -m unittest discover -s tests -v`。
