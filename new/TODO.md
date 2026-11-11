# 方案 C 待办（new/）

- [x] **T1 · `会议列表` 指令（2026-11-11 立项并完成）**：小A 对 bot 发「会议列表」，bot 经 advisor 用其已授权的日历 token 列出近期会议（含 9 位会议号、时间、是否组织者、是否进行中），供直接复制入会。规则式实现，不进 LLM，不进入会议模型上下文。范围：advisor 新增 `GET /meetings`（复用 FeishuCalendarValidator 读日历），bot 新增指令解析与格式化回复。
- [ ] **T2 · 闲聊/自由问答（待需求评审）**：与 v1 非目标（消息不进模型上下文）冲突；若要做需 REQUIREMENTS 变更评审（谁可聊、上下文边界、成本）。
- [ ] **T3 · Jira OAuth 接入（2026-11-11 立项）**：jira.nioint.com 实测为 Jira DC 9.12.13（≥9.11，原生 OAuth 2.0 授权码 + PKCE，无需 oauth2-proxy/OAuth 1.0a）。前置：Jira 平台管理员注册 OAuth client（⚙️ 管理 → 应用 → OAuth 2.0；redirect URI 精确指向 auth 节点 HTTPS 回调；scopes 只读）。范围：auth 凭据记录加 jira provider（DC OAuth 无 refresh_token，不复用单飞刷新语义，过期 → 重发授权链接）；advisor/bot 复用 NSDP token 方案的凭据管道与授权提示（NSDP 先行，方案对齐中）；使用点以 `JIRA_URL`/`JIRA_TOKEN` 注入。
