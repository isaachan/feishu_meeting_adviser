# 普通飞书会议状态观察

**电脑直接发起的会议：运行 `poll.py`。** 输入 9 位会议号后，它定期查询会议状态和参会人状态，把观察到的「会议开始」「人加入」「人退出」「会议结束」打印为 JSON 行。这些是**轮询得到的状态变化，不是飞书事件推送**；如果变化在两次查询之间发生，可能看不到，时间戳是观察时间。会议状态显示「进行中」时即输出「会议开始」，不代表精确的开始时刻。

原来的 `listen.py` 订阅 `vc.meeting.meeting_started_v1` 等四种**推送事件**；飞书公开文档注明它们只对通过 Open API 预约的会议产生，**不适用于验证电脑直接发起的普通会议**。

## 使用 poll.py

1. 给探针所用应用开通**应用身份** `vc:meeting.meetingid:read` **或** `vc:meeting:readonly`（用于 `GET /vc/v1/meetings/list_by_no` 按会议号查会议；以飞书接口的实际报错为准），以及 `vc:meeting.meetingevent:read` 或 `vc:meeting:readonly`（用于 `GET /vc/v1/meetings/:meeting_id` 查询状态和参会人），发布应用。应用的数据权限范围须涵盖你创建的会议。仅有上一轮为推送事件开通的 `vc:meeting.meetingevent:read` **不够调用 list_by_no**。
2. 在仓库根目录配置探针专用凭据（已有 `local.env` 可跳过复制和编辑）：

   ```bash
   cp studies/meeting-events/local.env.example studies/meeting-events/local.env
   # 编辑 studies/meeting-events/local.env，填入 BOT_APP_ID 和 BOT_APP_SECRET
   source studies/meeting-events/local.env
   python3 studies/meeting-events/poll.py 123456789
   ```

   把 `123456789` 换成自己的会议号。`poll.py` 只用 Python 标准库，无需安装 SDK；`local.env` 已加入忽略规则。
3. 保持终端运行：电脑直接发起这场会议 → 手机入会 → 手机退出 → 电脑结束会议并退出。查看每行的 `label` 和 `participant_id`；Ctrl-C 停止。程序默认每 3 秒查询一次（可用 `--interval 5` 调整）；已结束会议状态可能不会立即刷新。若提示 API 权限错误，核查报错中列出的**应用身份权限**是否已开通并发布。

官方接口：[按会议号查询](https://open.feishu.cn/document/server-docs/vc-v1/meeting/list_by_no)、[获取会议详情](https://open.feishu.cn/document/server-docs/vc-v1/meeting/get)。

## 用用户身份轮询（poll_user.py）

`poll_user.py` 走**用户授权**（PKCE）取得 `vc:meeting.meetingevent:read` 的 user access token，再轮询同样的“会议号 → 会议详情”链路，用于验证该用户身份权限能走多远。沿用上文的电脑/手机测试流程；输出仍是轮询状态，不是推送。

1. 前置：应用开通**用户身份**权限 `vc:meeting.meetingevent:read` 并发布；在应用安全设置中把 `http://127.0.0.1:8767/callback` 加入重定向 URL 白名单。
2. 运行（无需安装 SDK）：

   ```bash
   source studies/meeting-events/local.env
   python3 studies/meeting-events/poll_user.py 123456789
   ```

3. 首次运行会在终端打印授权链接：浏览器打开并用**参加会议的那个飞书账号**同意授权；回调后自动开始轮询。token 仅存于进程内存，约 2 小时过期，过期后重新运行重新授权（程序会提示）。也可用 `FEISHU_USER_ACCESS_TOKEN` 环境变量直接提供同应用、同 scope 的用户 token 跳过授权。
4. 授权时若提示缺少 scope，或轮询报 `99991672`/`121005` 等，说明该 scope 对应接口不足或数据不属于该用户——错误行会带具体 code 和 msg，以此判断 `vc:meeting.meetingevent:read` 在用户身份下能否完成按会议号查询与详情获取。
5. `list_by_no` 不接受 `vc:meeting.meetingevent:read`（无论应用还是用户身份，需 `vc:meeting.meetingid:read` 或 `vc:meeting:readonly`）。`poll_user.py` 在被拒（code 99991672/99991679）时自动改用 `GET /vc/v1/bots/user_active_meeting`（"用户正在参加的会议"，该 scope 可用）：要求**授权用户本人正在会中**才能解析出会议 ID；解析成功后会继续轮询详情直至会议结束，用户提前离会则观察不到后续事件。

## 仅针对 API 预约会议的推送探针

若另行验证通过 Open API 预约会议的四种推送，需在同一应用订阅对应事件并发布，然后安装 `lark-oapi>=1.4,<2` 和 `python-socks[asyncio]>=2.4.4,<3`，运行：

```bash
source studies/meeting-events/local.env
studies/meeting-events/.venv/bin/python studies/meeting-events/listen.py 123456789
```

这一探针只监听推送，不轮询普通会议；没有推送不能直接证明权限不足。
