# LLM-Gateway 执行链性能测量方案

状态：设计稿，待实现。目标是量化 OpenCode 冷启动、长期会话复用、skill/工具及模型推理各自的耗时，为是否自研轻量 agent 提供依据。本文没有实测结果，也不预设 OpenCode 一定是主要瓶颈。

## 当前可观测性

gateway 的任务结果已有 `queued_ms` 与 `execution_ms`，advisor 日志记录提交至结果的总耗时。`execution_ms` 涵盖进程启动、配置核验、OpenCode session 创建、prompt 提交、skill/工具调用、模型推理及轮询，无法据此拆出瓶颈。现有本地日志没有足够的真实推理样本，不能给出可信的性能结论。

## 指标与计时边界

使用单调时钟记录阶段起止点，统一输出毫秒数；阶段可缺省，但不得用零值表示“没有测到”。至少记录：

| 指标 | 起点 → 终点 | 说明 |
|---|---|---|
| `queue_ms` | 任务持久化 → worker 领取 | 与模型执行分开 |
| `process_start_ms` | 请求启动 OpenCode → 健康检查通过 | 复用轮次标记 `process_reused=true`，此项不适用 |
| `capability_verify_ms` | 首次配置/agent/MCP/provider/tool 核验开始 → 完成 | 保留失败阶段和错误码 |
| `session_prepare_ms` | 创建或恢复 OpenCode session 开始 → 可提交 prompt | 标记 `session_reused`、`session_rebuilt` 与重建原因 |
| `prompt_submit_ms` | 提交本轮 prompt 开始 → OpenCode 确认接收 | 不包含后续生成 |
| `decision_wait_ms` | prompt 确认接收 → 本轮最终消息完成 | 包含模型生成和工具调用；不能直接命名为纯模型耗时 |
| `tool_ms` | 单次受控工具调用开始 → 返回 | 按 `skill`、NSDP、Jira 分类；只记录名称、状态和耗时 |
| `execution_ms` | worker 开始 → gateway 任务终态 | 与现有字段一致，校验分段之和不超出总耗时 |
| `end_to_end_ms` | advisor 准备提交 → advisor 收到终态 | 含网络、排队、轮询和重试；与 gateway 内部时间区别展示 |

如 OpenCode 返回可验证的 token usage，另记本轮输入、输出及上下文 token；无法取得时标注 `unavailable`，不从字符数估算。记录进程峰值 RSS、CPU 时间、OpenCode 进程数和活跃会议数，以观察复用带来的资源成本。工具和模型耗时可能重叠，报告中不得把重叠段简单求和。

## 采集与隐私

任务结果 `timings` 保留低基数的阶段数字、布尔复用标记及失败阶段；原始观测事件进入 gateway 结构化日志，按任务 ID 与匿名会议关联键关联。日志不包含字幕、prompt、skill 正文、工具结果、组织者 token/PAT 或 OpenCode 凭据。会话 ID 与组织者 open_id 不作为高基数监控标签；按 backend、模型版本、冷/热启动、工具类型、成功/失败和输入规模分组汇总。

对超时、取消、进程启动失败和工具授权失败也记录已完成阶段，避免只统计成功样本造成偏差。计时开销通过假后端和合成 OpenCode peer 验证，不能显著改变执行期限或结果语义。

## 对照实验

在同一机器、同一 OpenCode/模型/skill 版本、同一权限和网络条件下，以脱敏的固定会议快照重复执行。每种场景至少 30 次有效样本，记录失败和重试数量；先预热网络与模型服务，再随机交错运行对照，避免时间段差异。

1. **冷启动基线**：现有每任务新进程、新 OpenCode session；分别测试无工具、加载 skill、NSDP 查询、Jira 查询。
2. **会议内复用**：同一会议连续多轮，记录首轮与第 2、5、10 轮；对照相同轮次的冷启动执行。另测两个并发会议，确认串行等待与资源占用。
3. **长会话**：增加字幕和工具历史至接近上下文预算，观察 token 增长、压缩/重建、延迟及输出有效率。
4. **故障样本**：注入取消、超时、OpenCode 断连和 gateway 重启，记录恢复耗时及重复执行/重建次数。

报告每组的成功率、p50/p95 延迟、各阶段分布、token、峰值 RSS 与 CPU；同时列出样本数、环境版本和置信限制。只有相同输入与功能约束下的结果可以直接比较。真实 NSDP/Jira 后端波动大，报告中应把工具耗时单列。

## 决策与验收

若复用后 `process_start_ms` 和 `capability_verify_ms` 消失，而 `decision_wait_ms` 仍占绝大部分，应优先优化 prompt、工具查询和上下文大小。若 OpenCode 的框架开销在热会话中仍稳定占显著比例，再为轻量 agent 做功能等价的原型对照；原型必须覆盖相同的 skill、工具权限、历史、取消及输出校验。决策以端到端 p95、成功率、资源消耗和维护复杂度共同判断，不只看单轮最快值。

验收交付包括：可从任务结果及结构化日志重建阶段耗时、一次可复现的冷/热对照报告、异常样本清单，以及按上述指标写明是否值得启动轻量 agent 原型。报告应注明实际环境和日期，不用设计稿中的假设代替实测。
