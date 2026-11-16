# 飞书会议事件与状态调研（已结束，2026-11-16）

目标：验证当前飞书应用权限能否获取**会议开始、会议结束、人加入、人退出**四类信息，重点覆盖**电脑客户端直接发起的普通会议**。

## 结论

1. **推送事件对普通会议不可用**。四个推送事件 `vc.meeting.meeting_started_v1` / `meeting_ended_v1` / `join_meeting_v1` / `leave_meeting_v1` 官方文档均标注「仅通过 Open API 预约的会议会产生此类事件」。实测（`listen.py` 长连接订阅）：电脑直接发起的会议**收不到任何事件**——与文档一致，不代表权限缺失。
2. **轮询路径可行**（`poll.py`，应用身份）。链路：`list_by_no`（9 位会议号 → 会议 ID）→ `GET /vc/v1/meetings/:id?with_participants=true`（状态 + 参会人）。实测跑通组合：应用身份 `vc:meeting.meetingid:read` + `vc:meeting.meetingevent:read`。输出的是**轮询观察到的状态变化**（默认 3 秒一查），不是飞书推送，两次查询之间的快速进出可能漏看。
3. **用户身份仅 `vc:meeting.meetingevent:read` 不够查会议号**（`poll_user.py`）：`list_by_no` 返回 99991679（该接口要求 `vc:meeting.meetingid:read` 或 `vc:meeting:readonly`，与应用身份相同）。已实现回退：`GET /vc/v1/bots/user_active_meeting` 用该 scope 可解析会议 ID，但要求**授权用户本人正在会中**。
4. **calendar API 拿不到会议 ID**：日程 `vchat` 结构只有 `meeting_url`（含 9 位会议号）和会前设置，无 VC 会议 ID；`advisor/src/advisor/calendar.py` 的 `_meeting_no()` 同样只从 URL 提取会议号。且电脑直接发起的即席会议根本没有日程。

## 两种会议的区别（本调研核心背景）

| | 通过 Open API 预约 | 电脑直接发起 |
|---|---|---|
| 创建方式 | `POST /vc/v1/reserves/apply`（需 `vc:reserve`） | 客户端即时会议 / 日历日程会议 / 个人会议室 |
| 日历日程 | 官方原文：不会生成日程、不会显示在日历中 | 日程会议有，即时会议无 |
| 事件 `meeting_source` | 4（开放平台会议） | 2（即时）/ 1（日程） |
| 四个推送事件 | 产生 | **不产生** |

官方文档：[预约会议](https://open.feishu.cn/document/server-docs/vc-v1/reserve/apply?lang=zh-CN)（注意返回的是预约 ID，「会议ID仅在会议开始后才生成」）、[会议开始](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/meeting_started?lang=zh-CN)、[会议结束](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/meeting_ended?lang=zh-CN)、[加入会议](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/join_meeting?lang=zh-CN)、[离开会议](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/leave_meeting?lang=zh-CN)。

## 工具与权限对照

| 工具 | 身份 | 权限（应用/用户身份需分别开通） | 用途 |
|---|---|---|---|
| `listen.py` | 应用（长连接） | `vc:meeting.meetingevent:read` + 订阅四事件 | 仅 API 预约会议的推送监听 |
| `poll.py` | 应用 | `vc:meeting.meetingid:read`（会议号→ID）+ `vc:meeting.meetingevent:read`（详情/参会人） | **普通会议轮询（本调研采用）** |
| `poll_user.py` | 用户（PKCE） | `vc:meeting.meetingevent:read` | `user_active_meeting` 回退 + 详情轮询 |

凭据放 `local.env`（模板 `local.env.example`，已被忽略）；`poll*.py` 仅标准库，`listen.py` 需 `lark-oapi`（见下）。

## 复现（poll.py）

```bash
source studies/meeting-events/local.env
python3 studies/meeting-events/poll.py 123456789
```

保持运行后按序操作：电脑发起会议 → 手机入会 → 手机退出 → 电脑结束会议。每观察到一次变化输出一行 JSON（`label` = 会议开始/人加入/人退出/会议结束）。API 报错会打印具体接口、HTTP 状态、飞书 code 与 msg（不含凭据）。离线单测：`python3 -m unittest discover -s studies/meeting-events -p 'test_*.py'`。

## 过程记录

- `listen.py` 实测普通会议零事件 → 查文档确认推送仅限 API 预约会议。
- `poll.py` 首跑 `list_by_no` 报 99991672（缺 `vc:meeting.meetingid:read` / `vc:meeting:readonly`）→ 开通 `vc:meeting.meetingid:read` 后跑通。
- `poll_user.py`（用户身份仅 `meetingevent:read`）`list_by_no` 报 99991679 → 加 `user_active_meeting` 回退。
- 权限名相同但身份不同不通用（如 `vc:meeting.meetingevent:read` 应用/用户两版）；`99991672`/`99991679` 分别为 scope 缺失与用户身份权限不足。
