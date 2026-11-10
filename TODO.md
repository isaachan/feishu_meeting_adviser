# 跨模块 TODO

模块任务分别见 [meeting-listener](meeting-listener/TODO.md) 和 [advisor](advisor/TODO.md)。详细实现与验证证据保存在 [实现记录](docs/IMPLEMENTATION_RECORD.md)。

- [ ] **端到端功能与性能验收。** 联合核对入会、采集、材料读取、模型决策和真实会中/单聊展示。功能通过与提问结束至可用回答 P95 ≤ 20 秒分别记录；20 秒是规划预算，尚未经端到端验收。
- [ ] **3.7 会中共享文档上下文。** meeting-listener 负责解析 `magic_share_started/ended` 并提供共享文档信息；advisor 负责资料读取、上下文更新、预算和发言判断。沿用 [共享内容调研](advisor/docs/SCREEN_SHARE_CONTENT_INVESTIGATION.md)的接口能力与实测边界。
- [x] **3.8 旁听模式（已放弃）。** 2026-11-09 实测后正式放弃：应用权限、用户 OAuth 与 `user_active_meeting` 均打通，唯一阻塞是 `bots/events` 的 120002 会议门禁——「允许智能体入会」开关仅对白名单账号可见，本账号不在白名单，实测持续轮询也拿不到任何事件数据。证据与替代方向（妙记会后转写、申请白名单、本地 ASR）见 [listener 研究](studies/listener/README.md)；若日后加白可复测。
- [ ] **STT 上传授权的错位防护。** 基于飞书组织者身份的授权核验通过后，音频内容与所选会议并不绑定：同时段另一场会议的组织者可选取自己的会议、转写实际来自其他会议的音频。首版接受该限制；后续如需收紧，考虑核验参会成员关系或会中音频指纹等方案。
- [ ] **llm-gateway 增加用户认证与按用户限额。** 首版网关按内部部署无认证运行，任务幂等与隔离仅靠全局 `request_id`，且仅限可信内网；接入公网或多用户前补充身份认证、任务归属校验和按用户并发/队列限额，并同步更新 [远程推理契约](docs/CONTRACT_REMOTE_INFERENCE.md)（背景见 [plan.md](plan.md)）。
