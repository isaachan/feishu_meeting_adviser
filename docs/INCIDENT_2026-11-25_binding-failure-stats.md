# 事故后续分析 · 「受邀者 29% / 组织者 12%」绑定失败率拆解

- **日期**：2026-11-25（INCIDENT_2026-11-25_session-stuck-running.md 当晚补充复核）
- **背景**：原报告 §5 提到「owner 为受邀者的会话绑定失败率 7/24（29%），组织者本人会话 3/26（12%）」。这个数字容易被读成「身份决定成败」的概率事件——直觉上绑定成败应该是 0% 或 100% 的确定性事件。本文对 10 个 `vc_meeting_id=NULL` 会话逐场拆解后确认：**直觉是对的，绑定确实是确定性的；29%/12% 是把三类互不相关的失败原因混进同一个分母造成的混杂统计**。
- **方法**：`data/prod/advisor/adviser.sqlite3` 与 `data/prod/logs/advisor.log`（47,457 行，2026-11-17 起）只读查询；飞书 VC API 只读复现（owner token 经 auth 节点 `/credentials/{open_id}/token` 常规刷新，仅 GET，与 observer 每 15 秒的运行时行为一致）。**未修改任何代码与数据。**

## 1. 绑定的真实判定条件（与身份无关）

prod 实测（2026-11-25 17:49，owner token）：

```
GET /vc/v1/meetings/list_by_no → code=99991679
  msg: required one of these privileges under the user identity:
       [vc:meeting:readonly, vc:meeting.meetingid:read]
```

即应用授权里只有 `vc:meeting.meetingevent:read`，缺 `list_by_no` 所需的 scope——**确定性、永久、与具体会话无关**（原报告 §2.1 的定性成立，此处补上了拒绝的具体原因）。`_lookup_denied` 置位后（meeting_observer.py:20,113-117），绑定只剩一条路：

> `user_active_meeting`（fallback，meeting_observer.py:90-97）在某个 15 秒轮询时刻返回**恰好一个** meeting_no 匹配的会议 → `_details` 的 participants 里 owner 以 status=2 出现 → `bind_vc_meeting` 无同号并发会话。

因此每场会话的成败单独取决于三个条件：

1. 会话存活期间，**owner 的飞书账号曾注册为该场会议的在会参与者**（轮询命中任意一次即绑，之后每轮直查会议详情，不再依赖 owner 在会）；
2. 不存在同 meeting_no 的并发 running/paused 会话（同号互斥，store.py:595-600，先绑者独占）；
3. 会话寿命 ≥ 一轮轮询且出现过候选。

「受邀者 / 组织者」只是与条件 1 相关的弱相关变量，不是机制本身。

## 2. 10 个 NULL 会话逐场归因

DB 实际 50 个会话（原报告写 51，行数差 1 不影响口径）：BOUND 40 / NULL 10；organizer==owner 26（NULL 3）、受邀者 24（NULL 7）——与原报告 3/26、7/24 完全吻合。

| 会话 | 角色/时长 | 日志证据 | 归因 |
|---|---|---|---|
| s-486f712fac 'Daily Loop' | ORG 0.4m | `sources` 无行（recorder 从未连接） | 建会 24 秒即杀，**从未入会** |
| s-16e358e59c（无标题） | ORG 17m | mic finals 仅 "Hello 测试测试"“一二三四五 测试”；loopback finals=0；双源 09:37:31 即停 | solo 录音自测，**没进任何 VC**，09:52 idle 收场 |
| s-02c112d8fb '会参谋沟通' | ORG 1.9m | 71 秒双源 finals=0，admin end | 静音干跑，**从未入会** |
| s-5324beba95 '项目过程管理例会' | INV 0.4m | `sources` 无行 | **从未入会** |
| s-009ea2c5bf '会参谋进展沟通' | INV 17.8m | 10 分钟双源 finals=0（10:40:59 双源同秒 stream_end） | 静音干跑，**从未入会** |
| s-293afbe2ae 'NT3 DPM weekly' | INV 39.5m | 与 s-4f8bf00341（Kai 同场录制，10:20–10:48）同号 193154615 并发重叠 | **同号互斥**（Kai 先绑） |
| s-d407ee5bcf 'DMT Weekly' | INV 0.2m | 与 s-7c01ff9566（15:12–16:03）同号 379883592 并发 | **同号互斥** + 12 秒即杀 |
| s-801d51bb05 'SynNova Steering' | INV 16.3m | 与 s-f89d2a3d38（Kai，16:01–16:47）同号 787181981 并发；本机几乎无音频（loopback acked=0） | **同号互斥**（Kai 先绑） |
| s-b4f9bae8e4 'NT3.x DMT meeting' | INV 73.7m | loopback **407 条真实会议 finals**（10:06–11:03）；**mic finals=0**；双源 11:03:5x 同停，11:19 idle 收场 | 真实与会但 **owner 账号不在参与者名单**：laptop 整场只收不发 |
| s-4d547f88（本次事故） | INV 82.2m | loopback 446 finals；mic 30 finals（**15:26–15:27 与 16:07–16:09 两段**） | 同上：owner 在 laptop 前说过话、听过会，但他的账号从未被 `user_active_meeting` 看到 |

全日志 0 条 `meeting observation unknown/failed/authorization unavailable`，outbox 无任何 owner_notice（仅 reply/subscription_notice）——**所有失败都走了无日志静默分支，凭证从未出过问题**。

## 3. 干净口径下的真实失败率

剔除测试干跑（5 场）与同号互斥（3 场）后：

- **组织者本人实际开的会：0/23 = 0%**。ORG 的全部 3 个失败都是「建会不入会」的自测垃圾。
- **受邀者实际与会：2/19 ≈ 11%**。仅剩的 2 个失败（b4f9bae8e4 + 本次事故）共同特征：**laptop 只当接收端，owner 的账号不是该场会议的注册参会人**（电话入会 / 共用账号 / 投屏机，具体方式无直接证据，维持原报告 §5 的假设标注，但见 §4 的排除性证据）。

即：真正的身份相关差异是「组织者一定用自己的账号开/入会；受邀者偶尔以非注册身份与会」，而不是 29% vs 12%。

## 4. 排除性证据（prod 只读 API 复现）

1. **fallback 本身工作正常**：17:50 实测 `user_active_meeting` 返回 Peng 当时正在参加的会议（359069605），participants 含 `ou_b9f89...` status=2——「账号在会」场景 15 秒内即可命中。
2. **排除账号级/会议系列级解释**：11-23 同系列 ONVO DMT（345082144，同组织者 ou_dd865）的 participants 里 Peng 在（status=4 已离会）——同一系列 11-23 绑定成功、11-25 失败，差异只能来自**该场次的入会方式**。
3. **排除进程/凭证/路径因素**：11-25 同一进程（11-24 14:50:44 起）同一 owner：09:31 / 11:25 / 13:24 三场 INV 全部绑定，仅 15:09 这场失败。
4. **排除时间窗口问题**：b4f9bae8e4 有 58 分钟音频、约 230 轮轮询仍未绑定；同日 16:01 / 17:14 的另外两场 INV 正常绑定。

## 5. 对原报告的修正（3 处）

1. **§5「同号第二场 4 场」应为 3 场**：可确证的并发同号互斥只有 293afbe2ae / d407ee5bcf / 801d51bb05；另外两个 NULL（486f712fac、5324beba95）的同号兄弟会话在不同日期，互斥不适用，它们是 0.4 分钟的无音频干跑。
2. **§1「mic 15:29 后即无更新」不准确**：mic 在 16:07–16:09 还有 22 条 finals（最后一条 16:09:21），与 sources.last_seen 16:09:20 吻合。Peng 在事故会话中后段仍在 laptop 前说话——这反而强化了「laptop 的飞书客户端登录的不是他的账号」的假设。
3. **§5「以前『录音不停』的会都对上了号（观测兜底可用）」不成立**：11-24 的 s-3a2f6a3030（IoT车顶箱联调，**已 BOUND**）就是反例，详见 §6。

## 6. 附带发现：11-24 的另一次「卡 running」事故（根因不同，症状相同）

s-3a2f6a3030（11-24 11:00–14:50，229.9 分钟，BOUND）时间线（advisor.log + jobs 表 + gateway.log 交叉）：

| 时刻 | 事件 |
|---|---|
| ~11:21 | 会议实际结束（loopback 内容停止，最后一条 reply 11:21:22） |
| 11:21:25 | 最后一次 `model job start`（gen 143） |
| 11:21:44 | 最后一次观测写入（`observed_at` 冻结，presence=present） |
| 11:21:46 | gateway 侧 gen 143 **成功完成**（execution_ms=20922，rma36d1cd…），但结果从未被 advisor 取回/应用（jobs.state=abandoned 系事后 drain 作废） |
| 11:21:44 后 | 该会话 supervisor 循环**静默停摆**：无 observation 日志、无 `supervisor iteration failed/crashed`、无 `lease lost`——观测、取结果、发起新 job、idle 检查全部停止 |
| 13:19:46 | loopback 停流（会后静音续传约 2 小时） |
| 13:34:46 | idle 截止应到点，**未触发**（无 supervisor） |
| 14:03:26 起 | 用户连发 20+ 次 `POST /admin/sessions/{id}/end`（200，进 draining，drain_deadline_at=14:26:46），**deadline 亦无人执行** |
| 14:50:44.772 | 进程重启（`server listening`；deployed-commit 时间戳 11-24 14:50 吻合——当次部署即为救火） |
| 14:50:44.819 | 新进程启动 47ms 后 startup sweep `drain deadline forced ended`，会话终結 |

含义：**「已绑定」并不保证 S4/观测兜底可用**。该会话 vc_meeting_id 已绑定、会议详情至今可查（实测 status=3、meeting_no 匹配、participants 含 Peng），若 supervisor 循环活着，S4 会在 11:21 后一轮轮询内结束会话。真实根因是 supervisor 循环线程静默死亡/挂死（嫌疑集中在 `executor.take_finished()` 取 gen 143 结果的路径，或 `require_session` 抛 `StorageError` 后的无日志 `return`，supervisor.py:116-117；两者都无法事后区分），与 11-25 事故的观测静默是**不同根因、同一症状**。11-25 事故若无人工止血，大概率也以「用户重启进程」收场。

## 7. 对修复方向的影响

原报告 §6 的 A/B/C 排序不变，但本次复核上调了两点权重：

1. **方案 C（静默分支可观测化）的覆盖面应扩大**：不止 `_observe()` 的无日志 return（candidates≠1、presence≠present、bind 互斥、meeting_no 不匹配），还包括 supervisor 循环本身的存续状态（观测停更 N 分钟即告警）。`observed_at` 长期不刷新是所有这些故障的公共先兆——3a2f6a3030 冻结 3.5 小时、事故会话 82 分钟从未写入，均可用同一条告警捕获。
2. **方案 B（静音不续期 idle）依然是唯一能兜住 11-25 形态的终局手段**，但对 11-24 形态（supervisor 死亡）无效——idle 检查同样跑在已死的循环里。两类形态共同指向：**会话级 supervisor 需要进程级看门狗**（定期巡检 running 会话的 observed_at / last job 时间，超期接管），可并入方案 C 一并设计。

## 8. 证据清单（全部只读）

1. `sessions`/`sources`/`jobs`/`outbox` 表查询：50 会话、10 NULL 的时长/双源形态/同号重叠/收场方式/observed_at 冻结时刻。
2. advisor.log：10 个 NULL 会话的 ingest start/stream_end、STT finals 分布（含 b4f9bae8 mic=0、事故会话 mic 两段 30 条）、3a2f6a3030 的 job/观测停摆与 14:50 重启轨迹；全日志 0 条 observation 相关行。
3. gateway.log / gateway tasks.sqlite3：gen 143 于 11:21:46 成功完成（timing_event rma36d1cd…）。
4. 飞书 API 只读复现（owner token）：`list_by_no`=99991679（缺 scope 明细）、`user_active_meeting` 实时正常、345082144（11-23 ONVO DMT）与 190773294（11-24 IoT）的 participants 名单。
