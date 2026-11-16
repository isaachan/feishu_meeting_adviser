# 普通飞书会议状态观察

**电脑直接发起的会议：运行 `poll.py`。** 输入 9 位会议号后，它定期查询会议状态和参会人状态，把观察到的「会议开始」「人加入」「人退出」「会议结束」打印为 JSON 行。这些是**轮询得到的状态变化，不是飞书事件推送**；如果变化在两次查询之间发生，可能看不到，时间戳是观察时间。会议状态显示「进行中」时即输出「会议开始」，不代表精确的开始时刻。

原来的 `listen.py` 订阅 `vc.meeting.meeting_started_v1` 等四种**推送事件**；飞书公开文档注明它们只对通过 Open API 预约的会议产生，**不适用于验证电脑直接发起的普通会议**。

## 使用 poll.py

1. 给探针所用应用开通**应用身份** `vc:meeting:readonly`（用于 `GET /vc/v1/meetings/list_by_no` 按会议号查会议），以及 `vc:meeting.meetingevent:read` 或 `vc:meeting:readonly`（用于 `GET /vc/v1/meetings/:meeting_id` 查询状态和参会人），发布应用。应用的数据权限范围须涵盖你创建的会议。仅有上一轮为推送事件开通的 `vc:meeting.meetingevent:read` **不够调用 list_by_no**。
2. 在仓库根目录配置探针专用凭据（已有 `local.env` 可跳过复制和编辑）：

   ```bash
   cp studies/meeting-events/local.env.example studies/meeting-events/local.env
   # 编辑 studies/meeting-events/local.env，填入 BOT_APP_ID 和 BOT_APP_SECRET
   source studies/meeting-events/local.env
   python3 studies/meeting-events/poll.py 123456789
   ```

   把 `123456789` 换成自己的会议号。`poll.py` 只用 Python 标准库，无需安装 SDK；`local.env` 已加入忽略规则。
3. 保持终端运行：电脑直接发起这场会议 → 手机入会 → 手机退出 → 电脑结束会议并退出。查看每行的 `label` 和 `participant_id`；Ctrl-C 停止。程序默认每 3 秒查询一次（可用 `--interval 5` 调整）；已结束会议状态可能不会立即刷新。若提示 API 权限错误，先核查 `vc:meeting:readonly` 是否已开通并发布。

官方接口：[按会议号查询](https://open.feishu.cn/document/server-docs/vc-v1/meeting/list_by_no)、[获取会议详情](https://open.feishu.cn/document/server-docs/vc-v1/meeting/get)。

## 仅针对 API 预约会议的推送探针

若另行验证通过 Open API 预约会议的四种推送，需在同一应用订阅对应事件并发布，然后安装 `lark-oapi>=1.4,<2` 和 `python-socks[asyncio]>=2.4.4,<3`，运行：

```bash
source studies/meeting-events/local.env
studies/meeting-events/.venv/bin/python studies/meeting-events/listen.py 123456789
```

这一探针只监听推送，不轮询普通会议；没有推送不能直接证明权限不足。
