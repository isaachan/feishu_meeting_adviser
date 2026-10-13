# 飞书会议内输出：先验证文字，再调查语音

核查日期：2026-10-13。**进展：用户确认单条会中文本消息已发送成功，远端参会者能在会议内（非私信）看到，且显示为机器人发出。**这一场测试不证明其他会议、展示速度、重试幂等性或 bot 能直接说话。当前只研究 v2；不回退 v1。

## 1. 首选：会中发送文本消息（与 IM 群消息不同）

官方[加入会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/join.md)文档明确指向[发送会中消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/message.md)接口。后者写明：

- `POST https://open.feishu.cn/open-apis/vc/v1/bots/message`，企业自建应用；机器人**须先加入该会议**；能力仍处于**灰度**；接口上限 **100 次/分钟**。
- `Authorization: Bearer <tenant_access_token>` 支持应用身份（也支持用户身份）；申请并获授**`vc:meeting.message:write`（发送会中消息和表情）**。这是独立于已有的 `vc:meeting.bot.join:write` 入会权限的权限；不要假定可直接用现有授权发送。
- JSON 请求体：`meeting_id`（**join 返回的长数字 ID**，非 9 位会议号），`msg_type: "text"`，`content`（文本），可选 `uuid`（幂等去重 ID；推荐由一次回答固定生成以防重试重复发送）。响应 `code=0`、`data.uuid`；非 0 按失败处理。
- 文档目前只列 `msg_type=text` 和 `reaction`（表情）。它称为“会中消息”，**未明确承诺它等于用户说的 comment、一定在某个指定 UI 区域展示、所有参会者都可见或会被转写**；这些必须现场验证。

### 最短可行性测试（**现在只规划，不自动发送**）

1. 用户已申请 `vc:meeting.message:write` 并在本次测试会议发送成功；其他租户/会议仍需核对权限和灰度。仅在参会者知情的测试会议中，主持人打开所需会议开关，按现有流程 `join`。可用 `stage0.py send-message "测试文字"` 传入一条不含敏感信息的自定义短消息；不带文字参数时发送固定的无敏感内容测试文本。`config.yaml` 不再读取 `message.text`；命令行参数可能留在 shell 历史或进程列表，**不要传真实会议原文**，也不要从环境变量读取内容，**不要把会议原文/ID/token 写入仓库或诊断**。
2. 最小本地脚本已提供并通过模拟 API 测试；**不会自动执行**。明确发送时从 `v2/` 运行 `.venv/bin/python scripts/stage0.py send-message`（默认文案「这是一条机器人会中消息测试，请忽略。」），或运行 `.venv/bin/python scripts/stage0.py send-message "自定义测试文字"`；它使用当前入会应用身份 token 和 `session.yaml` 中的内部会议 ID，发**一次** `msg_type=text`。可在配置中设置固定 `message.uuid` 供同一次请求的不确定结果安全重试；缺省每次生成新 UUID。成功时终端仅显示不含正文/ID 的提示；API 接受请求不等于已送达界面。若超时/返回异常，先核对会议 UI，**不能盲目再次执行**。
3. **本次已确认**远端在会议内看到机器人署名的文本；后续上线前如需广泛适用，再测试不同会议/终端的可见范围、错误和 `uuid` 相同重试行为。不要把本次结果称为专门的 comment 接口或语音能力。
4. 用发言结束 → 字幕可用 → 回答生成 → POST 返回 → **参会者实际看到文字**分别计时；输出发送与展示也必须计入当前 **20 秒**端到端预算（现有“15 秒字幕 + 5 秒 LLM”尚未预留输出时间）。

## 2. 直接发语音：当前不能按同一接口实现

上述[会中消息接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/message.md)没有 `audio`/音频流 `msg_type`，不能把 TTS 音频或 URL 当成 `content` 来假装会议发言。核查时，官方 Video Conferencing → bot 的 API 导航列出会中消息、倒计时、入离会及读取事件等，没有找到“注入机器人麦克风音频”的公开接口；这**不是**对整个飞书平台绝无语音方案的证明。若必须让参会者在会议音频里听见 AI，需要另外确认官方机器人音频输出/客户端 SDK 能力、灰度、权限、媒体接入、回声/自触发风险及 TTS/播放时延；在获得官方依据与会中实测前标记为**待证实、不可交付**。

对比参考：[飞书 IM 发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create.md)的 `POST /open-apis/im/v1/messages` 可发送 `audio`，但文档明确它发给**用户或群聊**（需 `receive_id` / `chat_id`），**不是会议机器人在会议里讲话**，也不应被当作“会议内 comment”成功的证据。

## 结论与下一步

`bots/message` 的**单条文本发送和远端会议内展示已由用户确认**；实际送达耗时及完整问答 20 秒目标仍待实测，重试幂等性需在接 LLM 回复时验证。语音另列 [0.9](TODO.md) 调查，不默认存在或复用 IM 音频接口。任何实际消息发送均须用户明确同意并在专用测试会议进行。
