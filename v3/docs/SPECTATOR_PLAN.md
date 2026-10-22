# v3 旁听模式落地计划

依据：《飞书智能体入会 · 接入手册（对外版）》「二、旁听模式」及「五、事件数据结构」。
本文是 v3 的实现契约：先讲清与 v2 的差异，再给出模块、数据流、错误处理、里程碑与验收标准。

## 1. 能力边界（来自手册）

旁听模式 = Agent **以用户身份**（User Access Token）实时获取会议内容：

| 维度 | 说明 |
|:---|:---|
| 可以做 | 实时获取字幕、会中聊天、参会人进出情况 |
| 不支持 | 实时推送（**必须轮询拉取**）；在会上发声 |
| 前提 1 | 用户本人正在会议中 |
| 前提 2 | 会议 owner 已开启「允许智能体入会」（安全设置） |
| 前提 3 | 飞书客户端 7.68+ |
| 前提 4 | 应用权限 `vc:meeting.meetingevent:read`（用户身份权限），数据权限范围按手册选「按条件筛选 → 会议的归属者 → 包含 → 与应用可用范围一致」 |
| 特性 | 会议内**不出现任何机器人**，其他参会者无感知；已全量开放，无需申请 |

## 2. 与 v2 的差异（为什么单开 v3）

| 维度 | v2（独立入会） | v3（旁听模式） |
|:---|:---|:---|
| 鉴权身份 | 应用身份 Tenant Token | **用户身份 User Token**（OAuth 授权码 + PKCE） |
| 进入会议 | Bot 调 BotJoinMeeting 入会（join/leave 生命周期） | **无入会**。用户本人在会，Agent 旁听 |
| 会议发现 | 配置文件写死 9 位会议号 | 运行时调 **GetUserActiveMeeting** 自动发现，可选多个会议之一 |
| 字幕获取 | 轮询 ListMeetingEvents（Tenant Token，前提 bot 在会） | 轮询 ListMeetingEvents（**User Token**，前提用户在会 + 会议开智能体开关） |
| 事件类型 | 以 transcript 为主 | transcript + **chat_received** + participant_joined/left |
| 会中发言 | 可发会中消息 | 不支持发声（手册明确）；结果输出到本机，IM 推送为后续项 |
| 采集窗口策略 | 固定起止窗口扫描 + 周期核对 | 手册口径：**首次全量，之后用 page_token 增量**；游标失效时一次受控重拉 + 幂等去重 |

复用 v2 的成熟模式但代码独立：错误分类（kind 枚举、脱敏报错）、限速与退避、检查点事务提交、page_token 循环检测、句子 ID 去重回退链、OAuth PKCE + 刷新轮换、私有目录与 0600 文件权限。v3 不 import v2。

## 3. API 契约

### 3.1 GetUserActiveMeeting

- `GET /open-apis/vc/v1/bots/user_active_meeting`，User Token 鉴权。
- 返回用户当前所在会议列表，每项含 meeting_id（长会议 ID）、meeting_no（9 位会议号）、meeting_title。
- 多个会议时由用户通过 `--meeting-id` 指定，缺省取第一个并明确打印选择。
- 响应外层形态手册未给出截图级细节（可能为 `data.meetings` / `data.meeting_list` 等，条目可能直接含字段或包在 `meeting` 对象里）。解析器**宽容兼容多种形态**，并在 M3 真机验收时固定实测形态；解析失败按 RESPONSE 错误终态处理，不猜测。

### 3.2 ListMeetingEvents

- `GET /open-apis/vc/v1/bots/events`，User Token 鉴权。
- 参数：`meeting_id`（**长会议 ID**，非 9 位会议号）、`page_token`（首次不传=全量；后续传上次返回游标=增量）、`page_size`（>0，默认 100）、`start_time`/`end_time`（毫秒时间戳，可选）。
- 频率上限参考 v2 实测：100 次/分钟 → 客户端强制最小请求间隔 0.7s（可配，下限 0.7s）。
- 事件按 `activity_event_type` 区分：`transcript_received` / `chat_received` / `participant_joined` / `participant_left` / `magic_share_started` / `magic_share_ended`；高频事件按 5 秒/100 条聚合下发，单条事件可含多个 item，逐条处理。
- 事件体兼容三种形态（手册旁听口径 + v2 实测租户口径）：
  1. 事件对象直接含 `activity_event_type` + `<type>_items` 数组；
  2. 顶层 `event_type=transcript_received`、payload 直接含 `text`（v2 实测）；
  3. `event_type=meeting.activity` + `payload.activity_event_type`（文档示例），payload 为 JSON 字符串或对象均可。
- 字幕时间兼容数字毫秒（字符串/数字）与 RFC 3339（v2 实测）；`user_type` 可能为声纹类型 100/101/102，只透传不解释。

### 3.3 去重键（与 v2 同源策略）

- 字幕：`sentence_id` → `event_id+序号` → `正文+说话人+时间` 指纹，三级回退；缺失 sentence_id 标记 `identity_degraded`。
- 聊天：`message_id` → 内容+发送者+时间指纹。
- 参会人：`participant.id + 方向 + 时间`。
- 存储 INSERT OR IGNORE 幂等：游标重置重拉同一批事件不产生重复行。

## 4. 架构与数据流

```
用户（本人在会中，会议已开「允许智能体入会」）
   │  ① OAuth 授权码 + PKCE（scope: vc:meeting.meetingevent:read + offline_access）
   ▼
OAuthManager（data/oauth/<app_key>.json，0600，刷新轮换一次一用）
   │  ② access_token（内存传递，不落库不打印）
   ▼
SpectatorClient ──③ GetUserActiveMeeting──► meeting_id（长 ID）
   │
   │  ④ 循环：ListMeetingEvents(page_token=checkpoint 游标)
   ▼
parse_page ──► TranscriptItem / ChatMessage / ParticipantChange（不可变 dataclass）
   │  ⑤ 事务：事件入库(去重) + checkpoint 推进，原子提交
   ▼
SQLite（data/spectator.sqlite3，WAL，私有目录）
   │  ⑥ 提交成功后 best-effort JSONL 打印（transcript/chat/participant）
   ▼
终端 / 后续消费者（LLM 问答、纪要，M4+）
```

模块划分（`src/feishu_spectator/`）：

| 模块 | 职责 |
|:---|:---|
| `models.py` | MeetingRef、TranscriptItem、ChatMessage、ParticipantChange、EventKind、epoch_ms/digest |
| `client.py` | User Token HTTP 客户端：active_meetings()、events()；错误分类 ApiError；限速；响应字节上限 |
| `oauth.py` | PKCE 本地回调授权、刷新轮换、状态文件（借 v2 模式，scope 换成 vc 权限） |
| `storage.py` | SQLite：meetings+checkpoint、events 去重、容量上限；原子 commit_page |
| `collector.py` | parse_page 多形态解析；Spectator 轮询循环：增量、退避、终态、一次受控游标重置 |
| `config.py` | 严格 YAML（未知键拒绝、600 权限校验）；**无 meeting_no** |
| `__main__.py` | CLI：login / oauth-status / logout / meetings / listen / status |

CLI 数据面：

```bash
python3 -m feishu_spectator login        # 一次性授权
python3 -m feishu_spectator meetings     # 列出当前所在会议
python3 -m feishu_spectator listen       # 自动发现会议并持续旁听，Ctrl-C 停止
python3 -m feishu_spectator status       # 检查点与统计（脱敏）
```

## 5. 关键设计决策

1. **内置 OAuth 而非 lark-cli**：手册的 `lark-cli auth login` 设备码流程面向“AI Agent 替用户跑通接入”的场景。v3 是常驻 Python 服务，采用 v2 已验证的授权码 + PKCE + localhost 回调，零外部工具依赖；lark-cli 作为人工排障备选写入 README。
2. **无 join/leave 生命周期**：旁听没有入会动作，v2 的 SessionManager/不确定相位机整体不存在。停止 listen 只是停止轮询；用户离会后下一次拉取按权限错误终态处理。
3. **增量模型从“固定窗口扫描”改为“游标增量”**：手册明确 page_token 增量口径；checkpoint 只保存游标与统计。游标被服务端拒绝（参数错误 + 已存游标）时，允许**一次**去掉游标的受控全量重拉，靠幂等去重吸收重复，并记录 `cursor_reset_count`；再次失败进入终态人工处理（沿用 v2 的保守策略，不宣称官方游标过期码）。
4. **终态与可恢复态分离**：
   - `DENIED`（403/权限码）→ 终态 `permission_or_presence`：手册排查清单口径是“用户不在会，或会议未开允许智能体入会”，提示人工核对，不无限重试；
   - `TOKEN` → OAuthManager 自动刷新一次并重放；刷新失败终态 `reauthorization_required`；
   - `NETWORK/RATE_LIMIT/SERVER` → 指数退避 + 抖动，上限 cooldown；
   - `RESPONSE`/PageFormatError → 终态，页面不提交（先解析后落库，失败页整体丢弃）。
5. **先持久化后打印**：输出是 best-effort，绝不作为消费确认（v2 同款原则）。
6. **脱敏边界**：错误与诊断只含 kind/HTTP 码/安全 request_id，绝不带 token、app_secret、响应原文；meeting_id 在 status 中只显示哈希前缀。
7. **幂等存储**：`(meeting_id, item_key)` 主键 + INSERT OR IGNORE，任何重拉都安全；容量按物理文件大小上限保护，超限拒绝新页并保留旧数据。

## 6. 里程碑

| 里程碑 | 内容 | 完成判据 |
|:---|:---|:---|
| M1 骨架 | 目录、config、models、client（含错误分类） | 单测覆盖解析与分类 |
| M2 采集链路 | OAuth、storage、collector 增量轮询、CLI | 假服务器单测：发现→增量→去重→断点续拉 |
| M3 真机验收 | 真实会议 smoke：授权、发现、旁听一场会 | 见 §7 验收清单 |
| M4 消费对接 | IM 推送结果 / LLM 问答接入（复用 v2 adviser 思路） | 另立计划，不在本 plan 范围 |

## 7. 验收标准（M3）

1. `login` 后 `oauth-status` 显示已授权且 scope 含 `vc:meeting.meetingevent:read`。
2. 用户在会中：`meetings` 返回该会议（含长 meeting_id 与标题）。
3. `listen` 首轮全量拉取历史事件，之后增量：同一句字幕不因游标重置重复打印两次以上（存储零重复）。
4. 说话期间新增字幕从 `end_time_ms` 到本机接收的延迟有统计（median/p95），记录进验收记录，不承诺 SLA。
5. 会中发一条聊天消息，`listen` 输出 `chat` 事件；进出会输出 `participant` 事件。
6. 用户离会后 `listen` 在权限错误下以 `permission_or_presence` 终态退出且可重入。
7. Ctrl-C 后重启 `listen`，从 checkpoint 游标续拉，无重复入库。
8. 全程错误输出无 token/secret/正文泄露（人工抽查）。

## 8. 风险与未验证行为

| 风险 | 应对 |
|:---|:---|
| `user_active_meeting` 响应形态未实测 | 宽容解析多形态；M3 首次真机核对后收敛，解析失败即终态报错 |
| User Token 下 events 的事件形态可能与租户口径不同 | 解析器兼容三形态；真机验收记录实际形态 |
| 权限错误码具体值未知（用户不在会 vs 开关未开） | 统一映射 `permission_or_presence` 终态 + 人工核对提示，不猜测细分 |
| page_token 有效期/跨进程稳定性未知 | 持久化游标 + 一次受控重拉 + 幂等去重；记录 `cursor_reset_count` |
| 会议中用户多端/多会议 | `meetings` 列表让用户显式选择；listen 自动取第一个时打印所选 |
| 旁听无法发声/推送 | 产品边界，输出仅本机；IM 推送留 M4 |

## 9. 安全与隐私

- 会议内容仅存本机私有目录（0700/0600），不提交仓库；测试全部用合成数据，不调用真实会议。
- token/refresh_token 只存在于 `data/oauth/`（0600，独占锁）；日志与异常一律脱敏。
- 遵守与 v2 相同的授权边界：仅在获授权的会议中旁听，终端输出按会议授权保管。
