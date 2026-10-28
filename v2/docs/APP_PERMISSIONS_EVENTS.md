# 飞书 App 最小权限与事件清单

目标：用全新的仅有机器人能力的 app，通过运行报错逐步补齐权限/事件，得到运行本项目所需的最小集合。每验证通过一项，记录一项。

验证方法：在开发者后台开通该项 → 发布新版本 → 运行对应命令确认不再报 99991672（无权限）等错误。

## 已验证权限

| Scope | 名称 | 用途 | 触发的接口 | 验证命令 | 状态 |
|---|---|---|---|---|---|
| `vc:meeting.bot.join:write` | 加入并离开会议 | 机器人加入/离开会议，及拉取会议事件列表（含转写、进出会、会中聊天） | `POST /open-apis/vc/v1/bots/join`<br>`POST /open-apis/vc/v1/bots/leave`<br>`GET /open-apis/vc/v1/bots/events` | `./join <meeting_no>` | ✅ 已验证（bot 成功入会） |
| `docx:document:readonly` | 查看新版文档 | 前提条件：开通任一云文档类 scope 后 app 才会出现在文档「添加文档应用」列表，可被添加为文档协作者；同时是用户 OAuth 声明 scope，用于读 Docx 元数据与纯文本 | `GET /open-apis/docx/v1/documents/{token}`<br>`GET /open-apis/docx/v1/documents/{token}/raw_content` | 文档「…」→「添加文档应用」搜索；consume 读 Wiki 引用文档正文 | ✅ 已验证（列表可见；用户身份读取 88 字正文成功） |
| `vc:meeting.message:write` | 发送会中消息与表情 | 会中发文本消息（bot 入会介绍语与模型回答的唯一会中发送通道） | `POST /open-apis/vc/v1/bots/message` | `./consume`（介绍消息成功发出） | ✅ 已验证 |
| `calendar:calendar:read` | 查看日历 | 用户 OAuth：列出日历元数据以取得明确绑定的 calendar_id | `GET /open-apis/calendar/v4/calendars` | `oauth login` → `context_probe --identity user --list-calendars` | 待验证（scope 已授予，列表接口未单独跑过） |
| `calendar:calendar.event:read` | 查看日程 | 用户 OAuth：读取指定日程描述及富文本中的文档引用 | `GET /open-apis/calendar/v4/calendars/{id}/events` | `./consume`（event_discovery ok，读到 description/description_rich） | ✅ 已验证 |
| `wiki:node:read` | 查看知识库 | 用户 OAuth：解析 Wiki 节点的实际类型与底层对象（obj_type/obj_token），非 Docx 则标记 unsupported | `GET /open-apis/wiki/v2/spaces/get_node` | `./consume`（wiki resolved → object_type=docx） | ✅ 已验证 |
| `offline_access` | 获取用户刷新令牌 | OAuth 返回 refresh_token 供持续刷新；需在权限管理开通，且安全设置「刷新 user_access_token」开关开启（无开关则默认开），发布生效 | `POST accounts.feishu.cn/oauth/v3/token`（刷新） | `oauth login`（凭据 scope 含 offline_access，state=ready） | ✅ 已授权；刷新调用待 token 过期后自动验证 |

## 已验证事件订阅

| 事件 | Event 类型 | 用途 | 验证方式 | 状态 |
|---|---|---|---|---|
| （暂无） | | | | |

## 预告待验证（按报错补充，未验证不开通）

| Scope | 用途 | 触发的接口 |
|---|---|---|
| `im:message:send_as_bot` | 以机器人身份发 IM 消息（notifications 配置了 IM 接收人时才需要） | `POST /open-apis/im/v1/messages` |

另：用户 OAuth 流程声明的 scope（`calendar:calendar:read`、`calendar:calendar.event:read`、`docx:document:readonly`、`wiki:node:read`、`offline_access`，见 `src/feishu_meeting_adviser/oauth.py`）需在 app 的"安全设置"重定向 URL 与权限管理中配置，待 OAuth 链路运行时逐一验证。

## 验证日志

- 2026-10-28: `./join 739285592` 报 `99991672 permission_or_admission_denied`（HTTP 400），定位为 `/vc/v1/bots/join` 缺 scope，待开通 `vc:meeting.bot.join:write`。
- 2026-10-28: 开通 `vc:meeting.bot.join:write` 后重新 `./join`，bot 成功入会，该项验证通过。
- 2026-10-28: 全新 app 在文档「添加文档应用」中搜不到；开通 `docx:document:readonly` 并发布版本后列表可见，确认文档应用入口需要云文档类 scope。
- 2026-10-28: `./consume` 报 `oauth_not_authorized`——新 app 尚无用户 OAuth 凭据，非接口权限问题。已在权限管理开通 `calendar:calendar:read`、`calendar:calendar.event:read`、`wiki:node:read`、`offline_access`（官方文档确认 offline_access 不开通则不返回 refresh_token），待重定向 URL 配置并发布后执行 `oauth login` 验证。
- 2026-10-28: OAuth 授权成功后 `./consume` 材料链路全通：event_discovery → description/description_rich → Wiki 解析为 Docx → 读到 88 字正文。`calendar:calendar.event:read`、`wiki:node:read`、`docx:document:readonly`（用户身份）验证通过；`oauth status` 确认 5 个 scope 全部授予（含 offline_access）。
- 2026-10-28: 介绍消息 `/vc/v1/bots/message` 报 99991672（此前 join 时已发过一次并标记 rejected，consume 时因材料先恢复未即时暴露）。开通 `vc:meeting.message:write`、`resolve-reply --outcome retry` 后介绍消息成功发出，验证通过。
