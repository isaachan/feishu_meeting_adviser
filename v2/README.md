# v2：应用机器人入会、字幕探测与持续采集

[计划](PLAN_OFFICIAL_MEETING_EVENTS.md) · [TODO](docs/TODO.md)

依据飞书[加入会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/join.md)及[会议事件列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events.md)接口：用应用身份执行 `bots/join`（输入 9 位会议号），**从入会响应直接获取内部会议 ID**，然后用相同应用身份拉事件。无需用户 OAuth，也不再使用 `list_by_no` 查 ID。接口处于灰度开放阶段；用户已在真实会议中观察到 `transcript_received`，并完成一场持续采集/双人脚本对照；多场增量可靠性与问答端到端时延尚未验收。**v2 是当前唯一方案，v1 因无可靠说话人信息、上下文不足而放弃。原 6 秒目标已撤销；新的规划目标是提问结束到可用回答 20 秒，暂分配约 15 秒获取字幕、5 秒 LLM 生成/输出。**这不是达标承诺：单场测试字幕片段的接收差值中位约 15 秒、最大约 24 秒，完整问答链路尚未实现。计时终点及统计口径见 [TODO](docs/TODO.md)。

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

## 双人测试：持续采集与本机调试记录

先由同意测试的两人按[双人对照脚本](docs/TRANSCRIPT_TEST_DIALOGUE.md)准备朗读；确保完成 `stage0.py join`，**不要在同一会议重复 join**。在 `v2/` 运行：

```bash
.venv/bin/python scripts/stream_transcripts.py
# 说话、记录时间并观察本机终端；结束时按 Ctrl-C
.venv/bin/python scripts/stage0.py leave  # 不再需要 bot 参会时执行；停止采集本身不会自动离会
```

- 采集器从 `v2/config.yaml` 读取可选 `stream` 设置（参考 `config.example.yaml`），默认每轮完成后等 2 秒，每次 events 请求至少间隔 1 秒（官方上限 100 次/分钟）；首次只回看最近 10 分钟。单轮最多 10 页、每页最多 100 条；每页解析、写盘并检查点后才继续，**不会把所有原始 payload 汇总或存盘**。它在本机终端打印新字幕，修订句标记 `[修订]`。
- `v2/output/meeting_<ID哈希>/state.json`（`600`）记录会议 ID、页游标、时间窗口与最多 5000 个去重键/文本摘要；同目录每次运行的 `run_*/transcripts.jsonl`（`600`）**保存真实字幕正文、说话人名称及脱敏 ID 摘要、句子/事件 ID、事件及接收时间**以便两人私下对照，`diagnostics.jsonl`（`600`）只保存请求耗时、字幕年龄、事件类型计数、分页/重复/错误/修订统计，**默认不存完整事件 payload、token 或原始请求响应**。如解析异常确需排查原始结构，可经参会者同意后在 `config.yaml` 显式设置 `stream.capture_failed_page: true`，仅在解析失败时将该页原始事件写入同目录 `failed_pages.jsonl`（`600`，极其敏感，可能含参会者信息），排查完立刻改回 `false` 并安全删除该文件。目录 `700` 且被 Git 忽略。日志单文件单次运行默认最多 10 MB，达到上限停止；不会自动删除历史日志，请在测试结束后按授权范围清理整个 `v2/output/`，勿把它复制到聊天或仓库。不能在两个进程中同时采集同一会议（文件锁防止状态覆盖）。
- 时间窗口以最新事件时间向前重叠 120 秒，每页写入后更新游标/去重状态，重启会尝试继续。但**飞书空页/历史留存与跨轮询游标语义尚未充分实测**：依赖重叠窗口减少漏句，而非保证零漏句；无时间戳/句子 ID 时只能降级去重，旧数据超过重叠窗口或去重上限也可能重现/漏掉。若进程在日志写入与状态提交之间崩溃，重启可能在日志里重复一条（日志至少一次语义）；若在状态提交与终端打印之间崩溃，终端可能少显示，但字幕仍可从本机日志核对。检查 `sentence_id`、时间与日志确认。
- 遇到限流/网络/5xx 最多退避重试 5 次，认证/权限/解析错误会停机并记录脱敏错误信息；普通模式仍隐藏服务端 `msg`；可独立运行 `stage0.py diagnose` 在本机排查，但它不一定带采集器当时相同的时间窗口和游标（采集器原游标仅保存在私有 `state.json`），所以不保证能复现相同错误。Ctrl-C **仅停采集，不退会**；`stage0.py leave` 成功后才删除本地 session。新会议需要重新 `join`，将生成隔离的会话状态。输出中有敏感会议资料，请确保同事同意并设定保留/删除期限。

旧的 `scripts/lookup_meeting_id.py` 仅保留作独立诊断工具，它使用会议列表接口并需要**额外权限**；正常入会→采集流程不需要它。持续采集已在**一场**获授权的真实会议中完成双人脚本对照，但尚未完成多场会议的严格延迟、漏句、分页和长期稳定性验收；详见 [TODO](docs/TODO.md)。自动化测试（不访问飞书）：`.venv/bin/python -m unittest discover -s tests -v`。
