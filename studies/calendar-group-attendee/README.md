# 日历群组参与人 × 小C 邀请名单准入调研（2026-11-19，已按文档结论实现，live 验证待补）

目标：小C 想入会，但 ta **不在日程的直接邀请名单里**，而是日程邀请的某个**群组**的成员——advisor 目前会拒绝（`not_invited`）。本调研判断该场景能否处理、用什么接口、改哪些模块，并用探针核实关键 API 事实。

## 结论（先说答案）

**能处理，且预期不需要新增 OAuth scope。** 飞书日历有专用的「获取日程参与群成员列表」接口（`GET /calendars/{calendar_id}/events/{event_id}/attendees/{attendee_id}/chat_members`），支持 `user_access_token`，权限要求与现行用户日历读 scope 同族；约束是**调用者必须在该群成员列表中**——恰好与小C 的场景自洽（小C 就是被邀群组的成员）。修复路径见下文「推荐方案」。

## Bug 定位（现行链路）

1. bot「入会」→ advisor `create_session`（`advisor/src/advisor/api.py:213-221`）：非组织者须 ① 在 supervisor 名单，② `attendees & caller_ids` 命中该场邀请名单，否则 `not_invited`（用户文案 `bot/src/bot/service.py:155`）。
2. `attendees` 由 `FeishuCalendarValidator._occurrence_attendees`（`advisor/src/advisor/calendar.py:334-373`）从 attendees 专用接口分页拉取，**只收集 user 类型的 id**；docstring 明示「non-user types (chat/room/third-party) carry no user id and are skipped」。
3. 因此**群组参与人不产生任何可命中的 user id**：群成员小C 即使在名单内且已授权，也必然 `not_invited`。这是准入数据源的语义缺口，不是偶发故障。

前置事实：整条链路用**小C本人的日历凭据**，`_window_events` 列的是小C自己的 primary calendar。群邀日程必须先出现在小C的日历里，才会走到 `not_invited` 这一步；若飞书根本不把群邀日程投到成员日历，错误会更早（`meeting_not_found`），修复思路完全不同。探针第 1 项就是区分这两种形态。

## API 事实（2026-11-19 官方文档核对）

### 1. 参与人（Attendee）资源的真实形状

[日程参与人资源介绍](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event-attendee/introduction)：

- `type` ∈ `user` / `chat`（群组）/ `resource`（会议室）/ `third_party`（外部邮箱）。
- `attendee_id`：参与人**在当前日程内**的唯一标识，按类型定前缀——`user_xxx` / **`chat_xxx`（官方明示「可用于查询群组内的群成员信息」）** / `resource_xxx` / `third_party_xxx`。
- user 类型另携带 `user_id`（受 `user_id_type` 查询参数控制，当前 advisor 用 `open_id` 域）；chat 类型另携带 `chat_id`（`oc_xxx`，IM 域群 id）；`rsvp_status=removed` 表示已移除。
- 每日程最多 3000 个参与人。

即：`_occurrence_attendees` 跳过的不是「无标识的杂物」，而是携带 `attendee_id=chat_xxx` / `chat_id=oc_xxx` 的可展开实体。

### 2. 专用接口：获取日程参与群成员列表（关键发现）

[文档](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/calendar-v4/calendar-event-attendee-chat_member/list)：

| 项 | 内容 |
|---|---|
| 路径 | `GET /open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees/{attendee_id}/chat_members`（`attendee_id` 用 participants 列表返回的 `chat_xxx`） |
| 身份 | `tenant_access_token` 或 **`user_access_token`**（当前身份由 token 类型决定） |
| 权限 | 任一：更新日历及日程信息 / **读取日程信息** / 获取日历、日程及忙闲信息——与 attendees 列表接口同一组权限族；现行用户 OAuth scope（`calendar:calendar:read` + `calendar:calendar.event:read`）已能调 attendees 列表（v2.5 实测过），**预期同样够用**（待实测确认） |
| 前提约束 | ① 应用开启机器人能力（bot 节点应用已具备）；② 当前身份须可查看该日程参与人列表（组织者，或参与人且日程 `attendee_ability` 允许）；③ **当前身份必须在该群成员列表中**，否则调用失败 |
| 分页 | `page_size` ≤ 100，`page_token`/`has_more`；频率 1000 次/分钟、50 次/秒 |
| 响应 items | `{rsvp_status, is_optional, display_name, is_organizer, is_external, open_id}`（id 字段受 `user_id_type` 控制；文档示例的键名与 attendees 列表的 `user_id` 不一致，探针以原始转储为准） |
| 典型错误码 | `194001` 无参与人列表查看权限、`194000` attendee 不存在、`193001` 日程未找到、`190002` 参数/资源权限 |

约束 ③ 与本 bug 场景天然自洽：**能用小C自己的 token 查到群成员的，只有小C所在的群**——被邀群正好是小C所在的群。也意味着「小C曾被拉群后又退群」时该群查不到（诚实拒绝）。

### 3. 备选通道（对比后不推荐）

- **IM 通用接口** `GET /im/v1/chats/{chat_id}/members`（[文档](https://open.feishu.cn/document/server-docs/group/chat-member/get)）：同样要求 operator（用户或 bot）在群内，且需 `im:chat.members:read` 等 **im scope（用户版）** + bot 能力——要在 CONTRACT_AUTH 加 scope、存量小C凭据全部重授权，劣势明显。且用 `chat_id`（`oc_xxx`）而非 `attendee_id`。
- **attendees 列表项内联 `chat_members` 字段**：Attendee 资源结构里有该字段（「群组类型参与人生效」），但列表接口文档示例的成员项不含用户 id、无分页说明，可靠性存疑，仅作实测时的对照观察点。

## 推荐方案（advisor 侧实现口径）

准入语义扩展为：**该场邀请名单 = user 直接参与人 ∪ 受邀 chat 参与人的群成员**。

1. `_occurrence_attendees` 在收集 user id 之外，同时收集 chat 类型参与人的 `attendee_id`（`rsvp_status=removed` 剔除），binding 增加 `chat_attendee_ids`。
2. `create_session` 准入：user 直接命中失败后，逐个 chat `attendee_id` 分页调 `chat_members`，扫 `caller_ids`（沿用 `user_id/open_id/union_id` 多键匹配口径）；任一群命中即视为受邀。
3. 失败语义：传输/服务故障 → fail-closed `auth_unavailable`（沿用 v2.5 口径）；「调用者不在群内 / 无参与人列表查看权限」这类**确定性拒绝**（错误码待实测固化）→ 该群不构成邀请证明，继续试其他群，全部未命中才 `not_invited`。
4. 懒调用原则不变：组织者路径、user 直接命中路径不发起 `chat_members` 调用（与 v2.5「组织者不拉 attendees」同构）。
5. 分页上限沿用 `ATTENDEE_PAGE_CAP` 风格（20 页 × 100）；截断时 fail-closed（截断只可能漏掉真命中者，绝不能据截断放行）。
6. 日志只记群数与成员计数，不落成员名单（数据最小化）。

## 契约影响

- `CONTRACT_ADVISOR_API` §2「小C 邀请名单数据源」段落（现 v2.5）增补为 v2.6（additive）：邀请名单包含受邀群组成员，经 `chat_members` 按场拉取；失败语义同 v2.5 fail-closed。
- `REQUIREMENTS` S6「该场日历邀请名单有小C」与 `STATE_MACHINES` §0 的同名语义同步扩展为「直接受邀或所在群组受邀的成员」。
- `CONTRACT_AUTH`：**预期无需变更**（复用日历读 scope）。若实测发现 `chat_members` 在用户 token 下被拒，再按 v1.6 先例处理（BASE 不动、请求集合扩展、存量凭据一次性提醒重授权）。
- bot `not_invited` 文案可顺带说明判定口径（直接或所在群组受邀）。

## 边界与风险

- **大群成本**：每个群最多 20 页请求，仅在「建会且直接命中失败」时一次性发生；日程参与人上限 3000，实际群规模可控。
- **attendee_ability**：日程若关闭「参与人可查看参与人列表」，非组织者调 `chat_members` 会 194001——此时该群对小C不可证明（等价未受邀），组织者路径不受影响；文案是否提示由实现定。
- **外部实体**：`third_party` 邮箱参与人、`is_external` 外部群/成员不参与展开（外部成员 id 亦只有 open/union 域，可保持现有多键匹配，不主动扩大语义）。
- **循环日程**：`chat_members` 沿用实例 `event_id`（`<uid>_<start>`），与 v2.4/v2.5 绑定一致，无新增处理（探针覆盖验证）。
- **快照时序**：建会瞬间以 API 实时成员快照为准，与「不要求 C 已实时入会」的既有口径一致。

## 实测（probe_group_attendee.py，仅标准库）

准备（一次性）：

1. 组织者账号建一个带会议链接的日程，**邀请一个群组**（小C在群内），不直接邀请小C；循环日程也可（探针支持 DAILY/WEEKLY/MONTHLY 展开），非循环最简。
2. 应用后台已登记 OAuth 回调 `http://127.0.0.1:8767/callback`（与 meeting-events 探针同一配置），用户身份日历读权限已开通（现行栈已具备）。

```bash
source studies/calendar-group-attendee/local.env   # BOT_APP_ID / BOT_APP_SECRET
python3 studies/calendar-group-attendee/probe_group_attendee.py 123456789
```

用**小C本人**（群成员、非直接受邀、非组织者）的飞书账号授权；令牌只留内存。也可 `FEISHU_USER_ACCESS_TOKEN=...` 复用现成 token。`--calendar-id` / `--event-id` 可手动钉死绑定跳过自动解析。

输出：每阶段一行 JSON（`phase` = auth / user_info / calendars / events / occurrence / attendees / chat_members / verdict），**原始响应全量转储、不做字段筛选**；`verdict` 行同时给出「现行 advisor 判定」与「群组展开后判定」。API 报错会打印接口名、HTTP 状态、飞书 code 与 msg（不含凭据）。

判读口径（5 个待确认问题，全部回答后才算实测闭环）：

1. `events` 阶段能否在小C的日历匹配到 meeting_no——决定 bug 形态是 `not_invited` 还是更早的 `meeting_not_found`（后者要另立修复思路）。
2. `attendees` items 里 chat 参与人的真实字段（`type`/`attendee_id=chat_xxx`/`chat_id=oc_xxx`，及内联 `chat_members` 是否出现）。
3. `chat_members` 在用户 token + 现有 scope 下是否 `code=0`；被拒时记录 HTTP/code/msg（194001 vs scope 类 99991672/99991679 要区分）。
4. `chat_members` items 的 id 字段名（`open_id` 还是 `user_id`）与 `user_info` 三 id 的对齐。
5. 循环日程实例 id（`<uid>_<start>`）在 `chat_members` 子路径下同样可用。

离线单测：`python3 -m unittest discover -s studies/calendar-group-attendee -p 'test_*.py'`。

## 状态

- [x] 文档调研与修复方案（本文）
- [x] **实现落地（2026-11-19，未经 live 验证）**，按推荐方案分四个 commit：
  - `34f6f30` advisor calendar.py：`_occurrence_attendees` 返回 (user ids, chat attendee ids)；新增 `chat_member_hit`（chat_members 分页展开、soft 拒绝语义、截断 fail-closed）；`_get` 增加 `soft` 通道（provider 拒绝可返回 None）。
  - `004d71b` advisor api.py：`create_session` 直接命中失败后经 `_chat_invite_hit` 懒展开；StubCalendarValidator 支持 `chat_attendee_ids`/`chat_members`。
  - `b24ddcc` 契约 v2.6（CONTRACT_ADVISOR_API §2 / STATE_MACHINES §0 / REQUIREMENTS S6）+ bot `not_invited` 文案。
  - 本 commit 回填调研状态。
  - 单测：advisor 237 项（`test_ingest` 因环境缺 `websockets` 无法导入，与本次改动无关、修复前即失败）、bot 40 项全绿。
- [ ] live probe 实测（上列 5 问）——实现完全基于官方文档口径，`chat_members` 的**真实错误码语义**（不在群内/attendee_ability 关闭时返回什么 code）与 id 字段名仍需实测确认；若实测发现确定性拒绝被误判为传输故障（表现为反复 `auth_unavailable`）或反之（不在群内却反复 soft 跳过导致正常 `not_invited` 之外的静默），回填本节并按需调整 soft 判定。
