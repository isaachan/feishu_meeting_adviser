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

## `use_audio` 与主持人麦克风状态对照实验（2026-11-17）

[获取会议详情 API](https://open.feishu.cn/document/server-docs/vc-v1/meeting/get) 的 `meeting.ability.use_audio` 描述是“是否使用音频”，位于**会议级** `ability` 对象中。文档标注 `ability` 仅在应用身份（`tenant_access_token`）且查询参数 `with_meeting_ability=true` 时返回。此探针改用**用户身份**，先验证该字段是否会出现；若不出现，就无法用当前用户权限验证它与麦克风的关系。

`probe_use_audio.py` 复用 `poll_user.py` 的 PKCE OAuth：用 `BOT_APP_ID` / `BOT_APP_SECRET` 生成授权链接、在本机接收回调并换取 `user_access_token`，令牌只留在内存。授权用户须本人在会中，探针调用 `user_active_meeting` 将会议号解析为会议 ID，再请求 `with_meeting_ability=true&with_participants=true`。请先在飞书开放平台为应用开通**用户身份** `vc:meeting.meetingevent:read`，并配置 OAuth 重定向地址 `http://127.0.0.1:8767/callback`；然后用飞书客户端发起或加入会议，再运行：

```bash
source studies/meeting-events/local.env  # BOT_APP_ID / BOT_APP_SECRET
python3 studies/meeting-events/probe_use_audio.py 123456789
```

终端会输出授权链接，请用**正在会中的飞书账号**打开并授权。也可预先设置 `FEISHU_USER_ACCESS_TOKEN` 跳过 OAuth；此时无需应用 ID / Secret。若回调端口不是 8767，用 `--port` 指定并在应用后台登记对应地址。已知 VC 会议 ID 时可加 `--meeting-id ID` 跳过 `user_active_meeting` 查询，但仍需传会议号作匹配校验。`--samples` 默认每阶段 2 次，`--interval` 默认 2 秒。此流程不要求应用身份的 `vc:meeting.meetingid:read` 权限。

程序先输出一行 `phase=preflight` 的 JSON，包含 `use_audio_present`、`use_audio`、`ability_keys`、会议中的主持人相关字段，以及参会人中含 `audio` / `mic` / `mute` / `speak` / `host` / `owner` 的字段和全部参会人字段名。若用户身份响应缺少 `use_audio`，立即报告并结束；`null` 不会被当作 `false`。若字段存在，再按提示由**同一位主持人**依次操作“静音 → 开麦 → 再次静音”，每次完成后按回车，末尾给出对照判断。

判读口径：三个阶段均稳定且 `use_audio` 不变，说明它不能反映**这次主持人麦克风切换**。若值按 `false → true → false` 变化，也只说明会议级字段与这次操作同步；应让另一名参会人保持音频连接并独立切换麦克风，进一步排除“会议中是否有音频”等会议级含义。字段缺失、值非布尔、阶段内不稳定或会议中途结束时，实验结果视为未知。此探针只读，不修改会议设置或现有服务。

## 过程记录

- `listen.py` 实测普通会议零事件 → 查文档确认推送仅限 API 预约会议。
- `poll.py` 首跑 `list_by_no` 报 99991672（缺 `vc:meeting.meetingid:read` / `vc:meeting:readonly`）→ 开通 `vc:meeting.meetingid:read` 后跑通。
- `poll_user.py`（用户身份仅 `meetingevent:read`）`list_by_no` 报 99991679 → 加 `user_active_meeting` 回退。
- 权限名相同但身份不同不通用（如 `vc:meeting.meetingevent:read` 应用/用户两版）；`99991672`/`99991679` 分别为 scope 缺失与用户身份权限不足。
