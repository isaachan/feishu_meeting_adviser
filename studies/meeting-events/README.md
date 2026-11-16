# 飞书会议生命周期事件探针

监听四个飞书推送事件：`vc.meeting.meeting_started_v1`（开始）、`meeting_ended_v1`（结束）、`join_meeting_v1`（人加入）、`leave_meeting_v1`（人退出）。输出每条事件的 JSON 行（到达时间、事件 ID、会议号、操作者 open_id 等），用于逐项核对实际送达情况。只接收事件，不入会、不调用会中事件拉取 API。

## 准备

1. 在**正在使用的同一飞书应用**中，按事件订阅控制台提示开通**应用身份**（`tenant_access_token`）的「获取会议信息」`vc:meeting.meetingevent:read`，在「事件订阅」选择**长连接**接收方式并订阅以上四个完整事件名，然后发布应用版本。公开事件文档仍可能列出旧权限 `vc:meeting:readonly`；实际以当前控制台的推荐权限为准。相同权限名若开通的是**用户身份**版本（`user_access_token`），不能代替应用身份权限和事件订阅。
2. 准备一场**通过 Open API 预约**的测试会议；飞书文档明确标注这四类事件仅对这种会议产生。普通客户端发起/日历里直接创建的会议不适用于本项验证。可以一个人测试：先启动探针，使用飞书[预约会议 API 调试台](https://open.feishu.cn/document/server-docs/vc-v1/reserve/apply)调用 `POST /open-apis/vc/v1/reserves/apply`，取得会议号或入会链接；此接口另需 `vc:reserve` 权限。用自己的飞书客户端按会议号/链接入会，再主动退出或选择结束会议，观察开始、加入、退出、结束事件。单人退出时会议是否立即结束取决于实际会议行为，不能仅凭缺少某一条推送判定权限不足。
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
studies/meeting-events/.venv/bin/python studies/meeting-events/listen.py
# 只看某个会议号：追加 --meeting-no 123456789
```

每收到一条就向 stdout 打印一行 JSON；Ctrl-C 结束。`event_type` 和 `label` 可用来确认四种事件分别是否到达。`meeting_source` 仅在飞书提供时出现；`leave_reason` 仅人退出事件可能有值。建议核对飞书后台的事件订阅/推送记录和测试会议创建方式：**长连接成功或没有输出本身都不能证明权限已开通或缺失**。事件需在探针连接后发生，历史事件不会补发。终端输出含会议和用户标识，保存日志时自行妥善保管。

这里的「预约」仅产生有效会议号和入会链接，**不创建日历日程，也不等于会议已开始**；「开始」是随后有人真正进入该会议。探针要先连接，再由本人入会，才有机会接到开始/加入推送。若仅需检查事件处理器能否接收模拟数据，可使用开放平台事件订阅页的「发送测试事件」（若该页面提供），但模拟事件不能证明真实会议会产生推送。

官方文档：[开始](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/meeting_started)、[结束](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/meeting_ended)、[加入](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/join_meeting)、[退出](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/leave_meeting)。
