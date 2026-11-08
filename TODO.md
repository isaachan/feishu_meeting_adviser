# 跨模块 TODO

模块任务分别见 [meeting-listener](meeting-listener/TODO.md) 和 [advisor](advisor/TODO.md)。详细实现与验证证据保存在 [实现记录](docs/IMPLEMENTATION_RECORD.md)。

- [ ] **端到端功能与性能验收。** 联合核对入会、采集、材料读取、模型决策和真实会中/单聊展示。功能通过与提问结束至可用回答 P95 ≤ 20 秒分别记录；20 秒是规划预算，尚未经端到端验收。
- [ ] **3.7 会中共享文档上下文。** meeting-listener 负责解析 `magic_share_started/ended` 并提供共享文档信息；advisor 负责资料读取、上下文更新、预算和发言判断。沿用 [共享内容调研](advisor/docs/SCREEN_SHARE_CONTENT_INVESTIGATION.md)的接口能力与实测边界。
- [ ] **3.8 旁听模式。** meeting-listener 验证无独立机器人入会的字幕获取；advisor 通过自己的配置关闭会中回答并设置单聊接收人。联调时核对身份、会话和数据交接；当前独立研究见 [listener](studies/listener/README.md)。
- [ ] **llm-gateway 增加用户认证与按用户限额。** 首版网关按内部部署无认证运行，任务幂等与隔离仅靠全局 `request_id`，且仅限可信内网；接入公网或多用户前补充身份认证、任务归属校验和按用户并发/队列限额，并同步更新 [远程推理契约](docs/CONTRACT_REMOTE_INFERENCE.md)（背景见 [plan.md](plan.md)）。
