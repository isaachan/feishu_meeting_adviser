# 会议期间获取共享屏幕内容可行性调研（TODO 3.6）

调研日期：2026-10-29。方法：逐条核对飞书开放平台官方文档（VC 全资源目录、bot 会中事件拉取/推送契约、生命周期事件、录制与妙记 API、旁听模式官方指南），结合本机 25 场会话的诊断数据（仅事件类型聚合计数，未读取正文）。**除注明"实测"外，本文结论均为"手册/官方文档披露"，未在本租户实测。**本文不含代码改动**。

## 一、总结论

**会中获取共享屏幕的"画面"（截图/视频帧）——不可行：飞书开放平台没有任何返回会议画面内容的官方 API 或事件，机器人身份亦无例外。** 官方通道只提供共享的**状态与元数据**，且仅限两种形态：

1. **妙享（共享文档）事件**：会中活动事件含 `magic_share_started` / `magic_share_ended`，payload 只有操作人、`share_id`、`share_doc`（**url + title**）和时间戳，无任何画面数据。
2. **屏幕共享状态事件**：`vc.meeting.share_started_v1` / `share_ended_v1` 生命周期推送，payload 只有会议与人员元数据；且**仅对通过 Open API 预约的会议产生**——本产品按会议号入会的常规会议不适用。

普通"桌面/窗口"屏幕共享在 bot 会中事件里**完全不出现**（拉取与推送的 `activity_event_type` 枚举均只有 participant_joined / participant_left / transcript_received / chat_received / magic_share_started / magic_share_ended 六种）。

对 TODO 场景（发言人指着屏幕说"看这个"）的落地判断：

| 共享形态 | 会中能否获取内容 | 通道 |
|---|---|---|
| 飞书文档/表格等（经会议"共享文档/妙享"） | ✅ 可行（读文档本身，非画面） | `magic_share_started` 的 `share_doc.url` → 现有 docx/wiki（+新增 sheets）读取链路 |
| 桌面/窗口/浏览器等原始屏幕共享 | ❌ 无官方通道 | 无；不引入本地截屏/录制 |
| 会后回看（任意共享形态） | ✅ 会后可行 | 云录制文件 API、妙记媒体下载 API（均非会中） |

## 二、逐通道核查（官方文档依据）

### 2.1 会中活动事件（本产品现行通道）

- 拉取 `GET /open-apis/vc/v1/bots/events`：[会议事件列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events)。手册明示"including ... sharing events"，实际枚举即上节六种；`magic_share_started_items` 仅含 `operator` / `share_id` / `share_doc{url,title}` / `time`。
- 推送 `vc.bot.meeting_activity_v1`：[In meeting activity event](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/supportEventsList/meeting_activity)。事件体 `activity_event_type` 枚举与拉取**完全一致**（含 magic_share 两种），外层多包 `meeting_activity_items`；官方标注 limited rollout（灰度，需联系技术支持），推送方式 Webhook/长连接；5 秒/100 条聚合口径来自接入手册（见 [字幕延迟调研](TRANSCRIPT_LATENCY_INVESTIGATION.md)）。
- 权限：两条通道均为 `vc:meeting.bot.join:write`（应用身份，**本产品已持有**）或 `vc:meeting.meetingevent:read`（用户身份）任一；频率 100 次/分钟（拉取）。
- 官方旁听模式指南 [飞书智能体入会旁听](https://open.feishu.cn/document/mcp_open_tools/agent-best-practices/agent-join-feishu-meetings) 的能力清单同样只有字幕、会中聊天、参会人进出，未提及任何画面获取。

### 2.2 生命周期推送事件

- [开始屏幕共享 share_started](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/share_started?lang=zh-CN) / [结束屏幕共享 share_ended](https://open.feishu.cn/document/server-docs/vc-v1/meeting/events/share_ended?lang=zh-CN)：`vc.meeting.share_started_v1` / `share_ended_v1`，Webhook 推送，权限 `vc:meeting.meetingevent:read` 或 `vc:meeting:readonly`。事件体仅会议元数据（id/topic/meeting_no/时间/主持人）+ 用户对象，**无共享内容、无共享类型媒体字段**。手册明示"仅通过 Open API 预约的会议会产生此类事件"，与本产品"join 会议号"的使用方式不匹配。

### 2.3 VC 全资源目录（排除性核查）

[视频会议概述](https://open.feishu.cn/document/server-docs/vc-v1/video-conferencing-overview?lang=zh-CN) 列出全部资源与方法：预约会议（apply/update/get 等）、会议操作（get/invite/kickout/set_host/end）、录制（start/stop/get/set_permission）、会议报告、会议室/层级/配置、导出；事件仅 meeting_started/ended、join/leave_meeting、recording_started/ended/ready、share_started/ended。**没有任何"画面/帧/截图/媒体流"类接口**。bot 域另有 join/leave/message/events/user_active_meeting（旁听+独立入会），同样无媒体接口。

### 2.4 会后通道（非会中，记为备选）

- 云录制：`GET /vc/v1/meetings/:meeting_id/recordings`，需录制开启并在 `recording_ready_v1`（上传完毕）后才有文件链接；user_access_token + 录制权限。
- **妙记 API 已公开**（与 [字幕延迟调研](TRANSCRIPT_LATENCY_INVESTIGATION.md) 中"CN 无公开 API"的旧结论不同，本次核对确认已开放）：
  - [获取妙记信息](https://open.feishu.cn/document/server-docs/minutes-v1/minute/get) `GET /minutes/v1/minute/:minute_token`（元信息，5 次/秒）；
  - [下载妙记音视频文件](https://open.feishu.cn/document/minutes-v1/minute-media/get) `GET /minutes/v1/minute/:minute_token/media`，返回下载链接（**有效期 1 天**），错误码 `2091003 minute not ready, try later` 表明**转写完成后才可取**；需"下载妙记的音视频文件"等权限且对该篇妙记有阅读权限；
  - [导出妙记转写](https://open.feishu.cn/document/minutes-v1/minute-transcript/get) 等。
  - 妙记视频是否包含共享画面取决于妙记/录制设置，未经实测；即使包含也是会后产物，不满足"会中辅助理解"的 20 秒预算。
- 客户端 API（H5 JSAPI 列表）无会议媒体捕获类接口，手册亦无；`GetBotRealtimeEndpoint`（WebSocket）文档页仍非公开（portal API 返回 code=41404），且属推送接收通道，与内容无关。

## 三、实测确认状态

- ✅ 实测（既有）：本产品经 `bots/events` 拉取的 25 场会话里，collector 只持久化字幕，诊断里存在少量未分类（'other'）活动事件（单窗口 3 条），**无法判定是否 magic_share**；magic_share 事件的到达率、字段实况、暴露时延均未实测。拉取通道事件暴露延迟基线沿用 3.5 结论（中位 ~31s），magic_share 是否同样延迟未验证，预期同通道同样延迟；推送通道未接入。
- ❌ 未实测：`vc.meeting.share_started_v1` 实际到达（需 Open API 预约会议才可能）；妙记媒体/转写接口本租户权限与内容形态；slides 类文档读取（开放平台未见公开的幻灯片读取 API，docx/sheets 有读取接口）。

## 四、替代方案与决策

1. **放弃**"会中获取共享屏幕画面"：官方无 API（依据第二节清单），亦不做本地录制/截屏（TODO 明确排除）。
2. **另立实施项**（建议编号 3.7）：接入 `magic_share_started/ended` 事件 → 用 `share_doc.url/title` 经现有 Wiki/docx 解析链路（如为 sheets 需补 scope）读取共享文档正文，注入模型上下文；处理：仅覆盖飞书文档类共享、读到的是文档当前状态而非主讲人视角/页码、文档需对应用或授权用户可读、上下文预算与截断策略、隐私告知（会中会读取被共享文档）。预期收益：共享飞书文档的"看这个"场景可答；时延预期为事件通道时延 + 1-2 次文档读取。
3. **会后补偿备选**：录制文件 / 妙记媒体（含共享画面与否取决于录制设置），仅用于会后复盘，不做会中承诺；需要时再立独立项。
4. 桌面/窗口等原始共享场景：模型侧策略为字幕 + 主动追问澄清；可提示发言人改用飞书文档共享以便助手读取（产品话术，非 API 能力）。

## 关联

- 字幕通道与时延基线：[TRANSCRIPT_LATENCY_INVESTIGATION](TRANSCRIPT_LATENCY_INVESTIGATION.md)
- 现有权限清单：[APP_PERMISSIONS_EVENTS](APP_PERMISSIONS_EVENTS.md)（`vc:meeting.bot.join:write` 已持有，无需新增即可收 magic_share 事件）
