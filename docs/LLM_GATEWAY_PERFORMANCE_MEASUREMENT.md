# LLM-Gateway 任务耗时观测方案:advisor 提交 → 收到 reply

状态:设计稿,待实现。核心目标是把 advisor 从下发推理任务到收到 reply(或失败终态)的时间消耗拆解成可归因的阶段,量化 gateway 框架开销、模型推理、skill 与工具各自的占比,支撑后续优化决策(含是否自研轻量 agent)。本文没有实测结果,也不预设 OpenCode 一定是主要瓶颈。

## 1. 端到端时间线(现状)

一次决策的完整路径,即本文的分解对象:

```text
advisor 决定调用模型
  → POST /v1/inference-jobs            advisor 计时起点
  → 任务持久化(SQLite,state=queued)
  → worker 领取(next_queued)
  → 启动 backend:guardian(python) → opencode(node),等待健康检查
  → 配置核验(config/agent/mcp/provider/tool 五个 GET)
  → 创建 session(POST /session)
  → 提交 prompt(POST prompt_async)
  → 模型推理 + skill/工具调用(gateway 每 0.2s 轮询 message)
  → 输出校验 → 任务终态写入 store
  → advisor 轮询发现终态(每 0.2s)→ 解析 Decision   advisor 计时终点
```

## 2. 现有可观测性与缺口

- gateway 已有 `queued_ms`(store 墙钟差)与 `execution_ms`(worker 单调钟);advisor 已记录 `elapsed_ms` 并把 gateway 的 timings 回传到 `metadata.remote_timings`。
- 缺口:`execution_ms` 覆盖进程启动到终态的全部,无法拆分;工具调用无计时;日志非结构化,无法按任务重建时间线。

## 3. 指标定义

通用规则:同进程内一律单调时钟;阶段可缺省(省略字段),不得用零值表示"没测到";毫秒输出。每个指标须同时记录观测方法,凡靠轮询观测的边界须在报告标注量化精度(gateway 与 advisor 轮询周期均为 0.2s)。

### 3.1 顺序段(之和应 ≤ execution_ms)

| 指标 | 起点 → 终点 | 观测方法与说明 |
|---|---|---|
| `end_to_end_ms` | advisor 发起提交 → 收到终态 | advisor 单调钟,对齐现有 `elapsed_ms`;含网络、排队、轮询、重试 |
| `advisor_submit_ms` | 发起 POST → 收到响应 | advisor 侧;含网络 RTT |
| `queue_ms` | 任务持久化 → worker 领取 | **墙钟例外**:store 的 `created_ns/started_ns` 即墙钟,任务可能跨 gateway 重启重新入队,单调钟无意义;保持现状并标注 |
| `process_start_ms` | Popen guardian → `/global/health` 通过 | backend 内单调钟;含 guardian 启动与 stdout origin 解析(50ms 粒度) |
| `verify_config_ms` / `verify_agent_ms` / `verify_mcp_ms` / `verify_provider_ms` / `verify_tool_ms` | 各 GET 发起 → 返回 | 五段分开;`/provider` 可能拉取远程模型目录,属网络开销而非框架开销,报告中单列 |
| `session_prepare_ms` | POST /session → 拿到 session_id | backend 内 |
| `prompt_submit_ms` | POST prompt_async 发起 → HTTP 返回 | backend 内。注意:OpenCode 无"确认接收"回执,POST 返回只代表请求被服务进程接受 |
| `prompt_visible_ms` | prompt POST 返回 → 用户消息出现在 message 列表 | 轮询观测,±0.2s 量化;与 `prompt_submit_ms` 分开,不得合并解读 |
| `decision_wait_ms` | prompt POST 返回 → 最终 assistant 消息 completed | 轮询观测,含 ≤0.2s 量化;包含模型生成与工具/skill 调用,不能当作纯模型耗时 |
| `reply_validate_ms` | 拿到最终消息 → 决策 JSON 校验完成 | 通常可忽略,用于排除校验本身的开销 |
| `finish_gap_ms` | 任务终态写库 → advisor 轮询发现 | advisor 与 gateway 时钟独立,用各自墙钟时间戳对齐;含 advisor 0.2s 轮询量化 |

顺序链校验:`process_start_ms + 五个 verify 段 + session_prepare_ms + prompt_submit_ms + decision_wait_ms + reply_validate_ms ≤ execution_ms`(容差取轮询粒度)。

### 3.2 工具与 skill(与 decision_wait_ms 重叠,不参与顺序链校验,不得与模型耗时简单求和)

| 指标 | 观测方法 | 精度 |
|---|---|---|
| `tool_ms`(NSDP/Jira) | 在 `nsdp_tool_server` / `jira_tool_server` 内部用单调钟记录每次调用;按工具名、状态分类 | 精确,不经轮询 |
| `skill_ms` | skill 是 OpenCode 进程内行为,只能靠轮询 message 的 tool part 状态变迁观测 | ±0.2s 量化;调用本身 <1s 时相对误差大,报告中须注明 |

### 3.3 资源

进程树为 guardian(python) → opencode(node) → MCP server(python) → noam。macOS 无 /proc,峰值 RSS 与 CPU 用 `ps` 按固定间隔(建议 250ms)采样进程组;报告注明采样频率与峰值低估的可能;同时记录进程组存活进程数。token usage 的可得性验证已移出本方案(TODO T15),不在本轮指标内。

## 4. 采集与隐私

- 任务结果 `timings` 扩展为上述低基数阶段数字与失败阶段;`timings` 为 JSON 字段,向后兼容。advisor 经 `remote_timings` 自动透传,无需新契约。
- 原始观测事件进入 gateway 结构化日志(JSON,带 `request_id`;会议关联用 metadata 中已有的 `session_id` 作为关联键,不作为监控标签)。advisor 侧对应记录 `end_to_end_ms` 及各衔接点。
- 日志不包含字幕、prompt、skill 正文、工具结果、组织者 token/PAT 或 OpenCode 凭据。汇总分组仅用低基数维度:backend、模型版本、冷/热启动、工具类型、成功/失败、输入规模分桶。
- 超时、取消、进程启动失败、工具授权失败同样记录已完成阶段,避免只统计成功样本。计时插桩先用 fake backend 与合成 OpenCode peer(`tests/helpers/fake_opencode_server.py` 已有)验证不改变执行期限与结果语义。

## 5. 实施范围

本方案只做纯插桩,不改变执行行为:backend 阶段计时、verify 五段拆分、MCP server 工具计时、scheduler timings 扩展、gateway 结构化事件日志、advisor `elapsed_ms` 对齐命名。完成后每个任务可从结果与日志重建完整时间线,直接回答"时间花在哪里"。

依赖会话/进程复用机制的测量(`process_reused`、`session_reused` 等复用标记,会议内多轮与长会话实验)不在本方案内;是否推进,待本文实测结果出来后再决定(复用机制本身见 `OPENCODE_MEETING_SESSION.md`,尚未实现)。

## 6. 数据来源:真实会议观测

不做受控对照实验,不消耗合成调用;插桩上线后从真实会议被动采集。样本随会议自然累积,单个场景建议凑满 ≥30 个有效任务后再下结论。

- 观测期内尽量保持环境稳定(同机、同 OpenCode/模型/skill/noam 版本);版本或配置变更须在分析时注明,跨版本样本不直接比较。
- 失败与超时样本保留已完成阶段;分析时区分"被测系统失败"与"环境失败"(NSDP/Jira 限流、组织者授权过期、网络抖动),后者单独剔除并列入异常样本清单。
- 分析从 admin-site 每会话 timings 导出(§7)本地聚合:成功率、p50/p95、各阶段分布、峰值 RSS/CPU;真实 NSDP/Jira 后端波动大,工具耗时单列;所有轮询观测指标注明量化精度。只有相同输入与功能约束下的结果可直接比较。

## 7. 运维获取方式(admin-site)

测量数据分两层获取,均走现有运维面,不引入新部署组件。

**每会话明细导出(主要入口)。** advisor 持久化每个模型任务的 timings:gateway 返回的阶段数字与工具明细,加上 advisor 侧衔接点(`end_to_end_ms`、`advisor_submit_ms`、`finish_gap_ms`),按 job 行存入 SQLite(当前 timings 只用于 `tool_events` 判断、`elapsed_ms` 仅进日志,需补持久化)。新增只读端点 `GET /admin/sessions/{id}/timings`(CONTRACT_ADVISOR_API §9 additive bump,语义与 §9.6 transcript 导出同口径:无 owner 校验、无需幂等键、不存在 → 404),返回该会话全部任务行:job_id、request_id、提交时间、终态与 error_code、各阶段 ms、工具调用明细(名称/状态/耗时)及轮询量化标注。admin-site 会话列表每行提供「下载 timings」(JSON,可选 CSV),供本地分析;进行中会话可重复拉取,已结束会话数据不变。

**gateway 原始事件与对账。** gateway 结构化日志按 `request_id` 输出阶段事件(JSON lines);运维经 admin-site 现有 tail -f 日志流实时观察,或本地 grep/jq 聚合;gateway 任务表的 `timings` 为第二来源,用于与 advisor 导出对账(两边时钟独立,只对阶段数字,不对绝对时间)。

**汇总分析。** 不建看板:真实会议观测(§6)与日常分析均从导出文件本地聚合(p50/p95、分段分布);若实测后确有常驻趋势需求,另行立项。advisor 侧 timings 随会话数据同生命周期清理,不单独定保留策略。

边界与约束:导出仅含低基数阶段数字、工具名、状态与终态,不含字幕、prompt、skill 正文、reply 正文或任何凭据;沿用 §9 localhost/受信边界与无认证现状,部署面扩大前必须先加认证。每任务全量记录而非采样,单任务几十个数字,存储与导出开销可忽略。

## 8. 决策与验收

- 若 `decision_wait_ms` 占 `end_to_end_ms` 绝大部分,应优先优化 prompt、上下文大小与工具查询,而非更换执行框架。
- 若框架段(进程启动 + 核验)在全部样本中稳定占显著比例,再为轻量 agent 做功能等价原型对照;原型须覆盖相同 skill、工具权限、取消与输出校验。决策以端到端 p95、成功率、资源消耗和维护复杂度共同判断,不只看单轮最快值。

验收交付:任务结果与结构化日志可重建阶段时间线;一段真实会议时段的耗时分析报告(含各阶段分布、工具耗时单列与环境版本);异常样本清单;按上述指标写明优化优先级结论,以及是否需要开展复用相关的后续测量。报告注明实际环境与日期,不用设计稿假设代替实测。
