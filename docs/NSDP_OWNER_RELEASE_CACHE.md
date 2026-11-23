# NSDP release 查询按组织者共享：方案

状态：2026-11-23 已实现，待生产 timings 验收。本文件定义 `meeting_nsdp_lookup_release` 的结果复用；不改变现有推理接口、OpenCode 会话生命周期或 `noam nsdp info` 目录语义。

## 1. 目标与现状

同一组织者的多场 advisor 会议、同一场会议的多轮模型判断，重复查询同一 release 时复用结果，避免每次都运行 `noam nsdp releases --release <release>`。缓存仅作用于成功的工具结果，固定有效期 **60 分钟**；首次查询和到期后的查询仍需 noam。

当前 gateway 的 `MeetingCatalogs` 按 `session_id + owner_open_id` 保存一次成功的 `noam nsdp info` 候选目录；它不包含可作为 Gate 日期依据的 release 查询结果。每个模型任务会启动独立的 OpenCode/MCP 进程，而 `NsdpQuery._execute()` 又为每次 noam 子进程创建独立的临时 HOME/XDG 目录，因此现有 noam 本地缓存不会跨工具调用复用。长期 OpenCode session 方案见 [OPENCODE_MEETING_SESSION.md](OPENCODE_MEETING_SESSION.md)，但模型记住旧工具结果不等于工具层有可靠缓存。

## 2. 缓存语义

- **共享范围**：以受信任的 `metadata.owner_open_id` 为顶层隔离桶；它是 advisor session 的 owner（可能是会议组织者或获准入会的小C），不一定是日历组织者。不把 `session_id` 放入缓存身份。同一 owner 的不同会议可命中，其他 owner 绝不命中。工具入参 `release` 只负责选择桶内的结果，不能指定 owner。
- **结果索引**：桶内按经过现有 `_RELEASE` 校验、大小写规范化的 release **查询词**索引。`releases --release` 是大小写不敏感的子串过滤；`BL1.5` 与 `BL1.5.0` 是不同查询，不能共用一条结果。仅当前固定命令与默认租户下的结果可复用；若命令参数、租户或结果解释方式改变，升级缓存格式版本并清空旧条目。
- **写入与过期**：只在 noam 成功且通过现有输出检查后写入完整工具结果；`expires_at = fetched_at + 3600s`，读取不续期。错误、超时、空结果和未完成调用不进入结果缓存。到期即视为 miss，由后续请求重新查询；定期清理过期条目并限制每个组织者的条目数和结果总字节数。
- **凭据边界**：每次命中前仍按 owner 向 auth 取当前 NSDP 凭据；缺失时返回 `authorization_required`，不能用旧结果绕过授权。缓存只保存 owner 标识和凭据的 HMAC 指纹，不保存 token 明文；gateway 的 HMAC 密钥单独以私有权限持久保存，密钥丢失或轮换时清空结果缓存。发现凭据更换或删除时使该 owner 的旧结果失效；实时查询遇到 401 时同样清空并沿用现有授权通知。cache miss 时把已取得的凭据交给本次 noam 调用，避免再次向 auth 取同一凭据。若 token 在 NSDP 侧被撤销、auth 尚未得知且字符串未变，缓存无法立即察觉；最多使用到 60 分钟期限届满，这一新鲜度边界必须在上线验收中确认。
- **并发**：同一组织者、同一查询词的并发 miss 按 key 串行；等待者在首个查询成功后读取结果，不并发启动多个 noam。不同查询词或不同组织者互不阻塞。等待和锁的期限不得超过模型任务剩余 deadline；持锁期间不得占用 SQLite 写事务。首个查询失败后释放锁，不把失败写入 60 分钟缓存。

## 3. 放置与调用链

缓存由 **llm-gateway 的 NSDP 工具层**拥有，使用 gateway 私有数据目录中的 SQLite 存储；MCP 子进程通过 gateway 注入的可信路径访问，不接受模型传入的路径或 owner。与现有 `MeetingCatalogs` 使用同一数据库文件、独立表，便于沿用私有权限、重启恢复及定期清理。跨进程同键串行需使用进程安全的短事务/文件锁；noam 运行期间不持有数据库写锁。gateway 重启后，未过期且凭据未变的结果仍可复用。

```text
OpenCode → meeting_nsdp_lookup_release(release)
  → MCP 工具使用 gateway 绑定的 owner_open_id
  → 校验 release，向 auth 获取该 owner 当前凭据
  → 查 owner 桶中的规范化 release 结果
     ├─ 未过期且凭据一致：返回缓存结果
     └─ miss：同键获取查询锁 → 隔离环境运行 noam → 成功写缓存 → 返回结果
```

对模型返回的 NSDP 内容保持现有格式；不把缓存数据写进 advisor 的字幕、材料或模型任务输入。`noam nsdp info` 的按会议固定目录继续独立存在，作为版本候选，不用它代替实时 release 核验。此方案也不要求先完成长期 OpenCode session 复用。

## 4. 观测与故障边界

为区分“noam 慢”和“OpenCode/MCP 链路未完成”，工具调用至少记录：`cache_hit`、缓存年龄、auth 取凭据耗时、同键等待耗时、noam 子进程开始/结束及耗时、退出状态/超时、MCP 响应就绪；按现有 `request_id` 关联 gateway 任务。日志不含 token、命令结果、字幕或查询正文；工具结果仍按当前规则返回模型。记录在 noam 启动**之前**写出，避免超时或进程中断时只有完成态耗时。

现有 noam 子进程最长 60 秒，而 OpenCode 曾在约 220 秒时仍报告工具 `running`。缓存只能缩短重复且成功的查询，不能解释或解决首次 miss、MCP 通信停滞及 OpenCode 状态未更新；必须结合 `diagnostics.opencode_trace` 和上述工具内探针判断瓶颈。历史 timings 无法回填。

## 5. 实现与验收

gateway 的 `nsdp_cache.py` 在会议目录数据库中建立独立的 owner 凭据指纹和 release 结果表；固定 TTL 为 3600 秒，每个 owner 最多保留 64 条、2 MiB 结果。缓存锁是按 owner 和 release 分开的进程间文件锁，运行 noam 时不持有 SQLite 写事务。gateway 定期清理过期结果，重启后仍可读取未过期结果。HMAC 密钥保存在同目录的私有文件中，密钥丢失会清空旧结果。`nsdp_tool_server.py` 每次先向 auth 取该 owner 当前凭据；命中或 miss 都会核对凭据，变更、删除及实时查询的 401 会使旧结果失效。noam 仍使用每次调用独立、结束即删除的 HOME/XDG 目录。

`timings.tool_calls[]` 对 NSDP 工具补充 `cache_hit`、`cache_age_ms`、`cache_wait_ms`、`auth_ms`、`noam_ms`；`timings.diagnostics.nsdp_stages[]` 记录 `mcp_received`、`auth_start`/`auth_done`、缓存命中或未命中、`noam_start`/`noam_done`、`mcp_response_ready` 等无正文事件及时间戳。子进程在 noam 启动前写出阶段事件；若调用超时，可以用最后一个事件判断卡在哪一段。`mcp_response_ready` 表示工具已准备好响应，并不等于 OpenCode 已收到或处理完响应。

本地 gateway 全套单测运行 83 项，其中 5 项需要真实 OpenCode 模型的用例按默认配置跳过，其余通过。另以 `LIVE_OPENCODE_TEST=1` 运行 `test_nsdp_integration.py`：7 项全部通过（含上述 5 项真实模型用例，耗时 109 秒）；auth 和 noam 使用本地桩。测试覆盖跨实例复用、owner/查询词隔离、TTL、凭据变更与删除、401、失败不缓存、同键成功查询并发去重、重启恢复、等待超时、清理与首查并发、HMAC 密钥丢失，以及工具阶段数据进入 timings。生产环境尚未部署或取新 timings 验证；上线后应分别比较首次 miss 和重复 hit 的端到端时长，并用 `diagnostics.opencode_trace` 与 `diagnostics.nsdp_stages` 追查此前约 220 秒的首次工具卡顿。

