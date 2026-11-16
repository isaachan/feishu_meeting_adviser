# 飞书会议生命周期事件探针

输入一个会议号，监听四个飞书推送事件：`vc.meeting.meeting_started_v1`（开始）、`meeting_ended_v1`（结束）、`join_meeting_v1`（人加入）、`leave_meeting_v1`（人退出）。只打印该会议号的事件 JSON 行（到达时间、事件 ID、会议号、操作者 open_id 等）。只接收事件，不入会、不调用会中事件拉取 API。

## 准备

1. 在**正在使用的同一飞书应用**中，按事件订阅控制台提示开通**应用身份**（`tenant_access_token`）的「获取会议信息」`vc:meeting.meetingevent:read`，在「事件订阅」选择**长连接**接收方式并订阅以上四个完整事件名，然后发布应用版本。公开事件文档仍可能列出旧权限 `vc:meeting:readonly`；实际以当前控制台的推荐权限为准。相同权限名若开通的是**用户身份**版本（`user_access_token`），不能代替应用身份权限和事件订阅。
2. 准备要测试的 9 位会议号。**飞书公开文档注明这四种事件仅对通过 Open API 预约的会议产生**；可以先按下述电脑/手机流程实际试收，但普通客户端发起的会议如果没有推送，不能据此判断权限不足。
3. Python 3.11+ 环境安装 `lark-oapi>=1.4,<2`、`python-socks[asyncio]>=2.4.4,<3`（与 `bot/pyproject.toml` 一致）。例如在仓库根目录：

   ```bash
   python3 -m venv studies/meeting-events/.venv
   studies/meeting-events/.venv/bin/pip install 'lark-oapi>=1.4,<2' 'python-socks[asyncio]>=2.4.4,<3'
   ```

## 运行

在仓库根目录，复制本探针专用的凭据模板，填写与事件订阅应用一致的 `BOT_APP_ID`、`BOT_APP_SECRET`（`local.env` 已被 Git 忽略），再运行：

```bash
cp studies/meeting-events/local.env.example studies/meeting-events/local.env
# 编辑 studies/meeting-events/local.env，填入实际应用 ID 和密钥
source studies/meeting-events/local.env
studies/meeting-events/.venv/bin/python studies/meeting-events/listen.py 123456789
```

把 `123456789` 换成目标会议号。保持探针运行，按顺序操作：

1. 电脑启动这场会议；
2. 手机加入会议，再从手机退出；
3. 电脑结束会议并退出。

每收到一条就向 stdout 打印一行 JSON；看 `label` 对应「会议开始」「人加入」「人退出」「会议结束」，也可以用 `operator_open_id` 辨别操作者。Ctrl-C 停止。探针只接收连接后发生的事件，不能补收已发生的「会议开始」；如果会议号只能在电脑开会后才知道，那次的开始事件无法用本次运行验证。`meeting_source` 仅在飞书提供时出现；`leave_reason` 仅人退出事件可能有值。**没有输出不能直接证明权限不足**，还要对照飞书后台的推送记录与会议创建方式。终端输出含会议和用户标识，保存日志时自行妥善保管。

官方文档：[开始](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/meeting_started)、[结束](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/meeting_ended)、[加入](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/join_meeting)、[退出](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/leave_meeting)。
