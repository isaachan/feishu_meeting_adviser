# 推理任务组织者身份扩展（v1，2026-11-14）

本扩展适用于 advisor → llm-gateway 的内部推理接口；其余行为沿用
`legacy/docs/CONTRACT_REMOTE_INFERENCE.md`。两个节点须同步部署：旧网关不接受含组织者的摘要。

## 任务身份

- advisor 在 `metadata.owner_open_id` 中发送当前会话的组织者 open_id；它来自服务端会话记录，不能由会议文字或模型选择。
- 格式为 `ou_` 后接 1–128 个 ASCII 字母、数字、下划线或连字符。字段缺省兼容旧客户端，但不提供组织者专属工具能力。
- 若字段存在，`content_hash` 为以下对象的 SHA-256：`{system,input,output,backend,model,owner_open_id}`；序列化仍按原契约使用 sorted keys、`ensure_ascii=False` 和紧凑分隔符。缺省时保持原算法。
- gateway 校验字段格式与摘要；同一 `request_id` 不能绑定不同组织者。组织者身份与任务一同持久化，用于执行及重试；不传递用户 token。
- 当前内部接口没有服务间认证，因此只有受信任的 advisor 和运维进程可访问 gateway；摘要不是鉴权机制。跨机器部署应先增加服务间身份认证和网络隔离。

本扩展仅规定身份绑定，不代表 NSDP 工具或授权反馈已接入。
