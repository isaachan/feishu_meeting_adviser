# v3 飞书旁听模式（用户身份，无机器人入会）

> **已冻结（2026-10-22），仅存档。** 真机验证链路可用，但实测字幕延迟 ~25 秒，与 v2 在同一拉取通道（`bots/events`）测得的服务端暴露延迟一致，20 秒预算不可达；旁听模式无推送，低延迟需走 v2 的独立入会事件推送。结论见 [docs/SPECTATOR_MODE_CONCLUSION.md](docs/SPECTATOR_MODE_CONCLUSION.md)。会中输入源继续用 [v2](../v2/README.md)。

依据《飞书智能体入会 · 接入手册》「二、旁听模式」实现：Agent 以**你的身份**（用户 OAuth 授权）实时获取会议字幕、会中聊天和参会人进出；**会议里不出现任何机器人**，其他参会者无感知。与 v2（应用身份入会）互补，实现计划见 [docs/SPECTATOR_PLAN.md](docs/SPECTATOR_PLAN.md)。

## 前置条件（一次性）

1. 飞书开放平台创建企业自建应用，拿到 App ID / App Secret。
2. 申请权限 `vc:meeting.meetingevent:read`（**用户身份权限**）；【数据权限范围】选「按条件筛选 → 会议的归属者 → 包含 → 与应用可用范围一致」，变更后发布版本。
3. 在应用「安全设置」添加重定向 URL `http://127.0.0.1:8766/oauth/callback`（login 启动时会再次提示）。
4. 飞书客户端 7.68+。
5. 每场会议：owner 在会议「安全设置」开启「允许智能体入会」（找不到该选项先开 AI 总结）。
6. 使用时你**本人必须在会中**；旁听无实时推送，产品按手册口径轮询拉取。

备选：也可用官方 `lark-cli`（v1.0.55+）的 `auth login --scope "vc:meeting.meetingevent:read"` 设备码流程做人工排障；本产品使用内置 OAuth，不依赖 lark-cli。

## 安装与配置

Python 3.11+，本机 POSIX 文件系统。进入 `v3/`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

复制 [config.example.yaml](config.example.yaml) 为 `config.yaml` 并 `chmod 600 config.yaml`，只填应用凭据；**不需要配置会议号**，会议在运行时通过 `GetUserActiveMeeting` 自动发现：

```yaml
app:
  id: "cli_xxx"
  secret: "xxx"
```

## 使用

```bash
# 1) 一次性用户授权（浏览器完成，凭据只存本机 data/oauth/，0600）
python3 -m feishu_spectator login
python3 -m feishu_spectator oauth-status   # 核对授权状态

# 2) 列出你当前所在会议（返回长 meeting_id，供 --meeting-id 选择）
python3 -m feishu_spectator meetings

# 3) 持续旁听：自动选择第一个会议（多会议时打印列表，用 --meeting-id 指定）
python3 -m feishu_spectator listen
python3 -m feishu_spectator listen --meeting-id <长meeting_id> --no-text

# 4) 查看脱敏状态 / 显式查看最近内容
python3 -m feishu_spectator status
python3 -m feishu_spectator recent --limit 20 --show-text
```

`listen` 每轮拉取 `ListMeetingEvents`：首次全量，之后携带上次返回的 `page_token` 增量；字幕/聊天/参会人事件逐条解析为 JSONL 打印并去重入库（`data/spectator.sqlite3`，私有目录）。Ctrl-C 只停止轮询，不影响你的会议；重启后从检查点游标续拉，重拉同一批事件不产生重复。

## 行为边界

- 权限类错误（用户离会、会议未开「允许智能体入会」、权限不足）→ 终态 `permission_or_presence` 退出，提示人工核对，不无限重试。
- 网络抖动/限流/服务端错误 → 指数退避 + 抖动自动恢复；游标被拒时执行一次受控全量重拉（幂等去重），再失败进入终态。
- token 过期自动刷新（refresh token 一次一用轮换）；刷新失败提示重新 `login`。
- 旁听模式不能在会上发声；字幕等高频事件服务端按 5 秒/100 条聚合下发，本产品逐条处理。
- **字幕延迟为服务端暴露延迟**：`bots/events` 拉取通道从发言结束到事件可查约 25–40 秒且随场次波动（v2 于 25 场会议实测中位 ~31s、P95 ~40s，见 [v2 调研](../v2/docs/TRANSCRIPT_LATENCY_INVESTIGATION.md)），客户端轮询已接近最优（~1s 间隔）不是瓶颈。`listen` 停止时与 `status` 均输出最近 200 句的 `caption_latency_seconds`（median/p95/max）供量化核对；若需更低延迟需改用手册「三、独立入会」的事件推送通道（`vc.bot.meeting_activity_v1`，预期 ≈ ASR 出句 + 5 秒聚合）。
- 会议内容仅存本机，终端输出请按会议授权保管，不要重定向到公开日志。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

62 个测试全部使用合成数据（假 HTTP、假 OAuth、临时 SQLite），不调用真实会议。
