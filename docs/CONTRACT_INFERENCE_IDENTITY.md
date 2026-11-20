# 推理任务组织者身份扩展（v2，2026-11-20）

本扩展适用于 advisor → llm-gateway 的内部推理接口；其余行为沿用
`legacy/docs/CONTRACT_REMOTE_INFERENCE.md`。两个节点须同步部署。

**v2 变更（2026-11-20）**：`metadata.owner_open_id` 由可选改为**必填**；网关删除了
无组织者的降级链路（无工具 prompt），缺省或格式非法的请求一律 422 `invalid_request`。

## 任务身份

- advisor 在 `metadata.owner_open_id` 中发送当前会话的** advisor session owner** open_id（组织者小A 或名单内小C，先到先得；语义见 [STATE_MACHINES](STATE_MACHINES.md) v2 §0）；它来自服务端会话记录，不能由会议文字或模型选择。
- 格式为 `ou_` 后接 1–128 个 ASCII 字母、数字、下划线或连字符。**必填**；缺失或非法返回 422 `invalid_request`。
- `content_hash` 为以下对象的 SHA-256：`{system,input,output,backend,model,owner_open_id}`（含 `session_id` 时一并纳入）；序列化按原契约使用 sorted keys、`ensure_ascii=False` 和紧凑分隔符。
- gateway 校验字段格式与摘要；同一 `request_id` 不能绑定不同组织者。组织者身份与任务一同持久化，用于执行及重试；不传递用户 token。
- 当前内部接口没有服务间认证，因此只有受信任的 advisor 和运维进程可访问 gateway；摘要不是鉴权机制。跨机器部署应先增加服务间身份认证和网络隔离。

NSDP 工具按此身份绑定凭据；`tool_events` 中的 `nsdp_authorization_required` 随任务 timings 返回，由 advisor 触发组织者通知。

Jira 只读工具同样绑定组织者，从 auth 的 Jira provider 取 PAT，以隔离的 noam 子进程执行 issue、JQL 搜索、字段和项目查询。`jira_authorization_required` 随任务 timings 返回，由 advisor 通知该组织者提交 PAT；不阻塞会议，403 不触发重新授权。NSDP 和 Jira 通知各自去重。

## 会话上下文扩展（2026-11-14）

- advisor 在会话监督启动时异步调用 `POST /v1/context-sessions`，请求 `{"session_id":"s-…","owner_open_id":"ou_…"}`；202 表示已接受预热，不等待查询完成。若通知丢失，首个带会话身份的推理任务也会触发预热。
- 推理请求的 `metadata.session_id` 与 `owner_open_id` 一同标识会话，并同时纳入 `content_hash` 的 canonical 对象；`session_id` 仍为可选（缺省时 hash 不含该键）。与已绑定的组织者不一致时返回 409。
- gateway 按会话异步调用 `noam nsdp info`。每场会议第一次成功的目录固定不变；同一组织者另一场会议独立查询。重启后恢复持久快照；失败后按退避在后续请求重试。
- 每个推理任务首次提交时固定当时可用的目录或空值；重试仍使用原任务固定的内容。目录以 `reference_context.nsdp_catalog` 加入 OpenCode 输入，属于数据而非指令；不回显到任务公开 metadata。保留获取时间、来源及完整 `info` JSON。
- 预热不阻塞会议，目录就绪后不自动重跑冷启动期间的字幕。缓存按最后使用时间清理，默认 24 小时；活跃会议会在推理时续期。
