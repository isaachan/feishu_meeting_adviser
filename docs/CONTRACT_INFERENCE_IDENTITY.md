# 推理任务组织者身份扩展（v1，2026-11-14）

本扩展适用于 advisor → llm-gateway 的内部推理接口；其余行为沿用
`legacy/docs/CONTRACT_REMOTE_INFERENCE.md`。两个节点须同步部署：旧网关不接受含组织者的摘要。

## 任务身份

- advisor 在 `metadata.owner_open_id` 中发送当前会话的组织者 open_id；它来自服务端会话记录，不能由会议文字或模型选择。
- 格式为 `ou_` 后接 1–128 个 ASCII 字母、数字、下划线或连字符。字段缺省兼容旧客户端，但不提供组织者专属工具能力。
- 若字段存在，`content_hash` 为以下对象的 SHA-256：`{system,input,output,backend,model,owner_open_id}`；序列化仍按原契约使用 sorted keys、`ensure_ascii=False` 和紧凑分隔符。缺省时保持原算法。
- gateway 校验字段格式与摘要；同一 `request_id` 不能绑定不同组织者。组织者身份与任务一同持久化，用于执行及重试；不传递用户 token。
- 当前内部接口没有服务间认证，因此只有受信任的 advisor 和运维进程可访问 gateway；摘要不是鉴权机制。跨机器部署应先增加服务间身份认证和网络隔离。

NSDP 工具按此身份绑定凭据；`tool_events` 中的 `nsdp_authorization_required` 随任务 timings 返回，由 advisor 触发组织者通知。

## 会话上下文扩展（2026-11-14）

- advisor 在会话监督启动时异步调用 `POST /v1/context-sessions`，请求 `{"session_id":"s-…","owner_open_id":"ou_…"}`；202 表示已接受预热，不等待查询完成。若通知丢失，首个带会话身份的推理任务也会触发预热。
- 推理请求的 `metadata.session_id` 与 `owner_open_id` 一同标识会话，并同时纳入 `content_hash` 的 canonical 对象；缺省 `session_id` 保持上一版算法。与已绑定的组织者不一致时返回 409。
- gateway 按会话异步调用 `noam nsdp info`。每场会议第一次成功的目录固定不变；同一组织者另一场会议独立查询。重启后恢复持久快照；失败后按退避在后续请求重试。
- 每个推理任务首次提交时固定当时可用的目录或空值；重试仍使用原任务固定的内容。目录以 `reference_context.nsdp_catalog` 加入 OpenCode 输入，属于数据而非指令；不回显到任务公开 metadata。保留获取时间、来源及完整 `info` JSON。
- 预热不阻塞会议，目录就绪后不自动重跑冷启动期间的字幕。缓存按最后使用时间清理，默认 24 小时；活跃会议会在推理时续期。
