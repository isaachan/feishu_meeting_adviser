# 模型故障诊断

2026-10-14，根据用户反馈的间歇性 model_tools_unavailable 核查。

## 本次发现

本机托管诊断有一次工具检查错误，随后两次超时，没有成功保存决定记录：

| 时间（UTC） | 错误 | 本轮总耗时 |
| --- | --- | --- |
| 06:54:03 | model_tools_unavailable | 约 3.9 秒 |
| 06:56:03 | model_timeout | 约 90.1 秒 |
| 07:00:43 | model_timeout | 约 90.0 秒 |

此前日志只记录总耗时，无法确定两次超时发生在初始化、MCP 查询、提交轮次还是生成决定；不能将三次故障归为同一个原因。

使用 Codex CLI 0.162.0 与临时空目录，仅查询协议及创建 ephemeral 线程，未调用真实飞书，也未发送真实会议内容：7 个检查线程均显示 MCP runtimeStatus=disabled、工具目录为空；另一次合成空会议调用成功。修正检查后再调用合成空会议，默认模型 gpt-6-astra、reasoningEffort=high，约 24.4 秒返回沉默；6 个服务均 disabled。单次合成调用不代表实际会议性能或回答质量。

历史那次工具错误尚未真实重现。安装版本生成的协议 schema 明确 tools 可以是缓存目录，runtimeStatus 才表示线程连接状态；旧实现仅用“目录非空”判定隔离失败，既可能误报，也会漏掉目录为空但服务已 connected/starting 的情况。

现检查要求该线程枚举到的所有 MCP 服务明确为 disabled；已禁用服务的缓存目录不会阻断，connected/starting/未知状态仍阻断。工具请求及执行事件继续被拒绝。未更改用户全局 Codex 配置，未放开模型工具权限。

依据：[OpenAI 官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)；本机通过 app-server generate-json-schema 生成的 ListMcpServerStatusResponse 中，runtimeStatus 与 toolsError 的字段说明。官方文档明确传入 threadId 使用该线程连接，detail=toolsAndAuthOnly 省略资源。

## 怎样看下一次错误

修改加载需重启终端 B；终端 A 可以继续采集：

```bash
python3 -m feishu_meeting_adviser consume --backend codex --watch
```

输出新增 model_stage，不需要打开正文调试：

| model_stage | 含义 |
| --- | --- |
| startup / initialize | 启动本机进程与初始化协议 |
| account_read | 核对订阅身份 |
| mcp_discovery | 获取继承的服务目录并准备禁用配置 |
| thread_start | 建立带禁用配置的线程 |
| mcp_verify | 核对该线程服务确已禁用 |
| turn_start | 提交结构化模型轮次 |
| generation | 等待模型决定 |
| thread_cleanup | 决定完成后释放 ephemeral 线程 |

model_state=waiting 时，model_stage 保留上一次调用的阶段；model_error 也保留到后续决定成功。running 时的旧 model_error 不能当作本次新调用已经失败。

详细记录在配置数据目录的 diagnostics/diagnostics.jsonl（旧记录可能已轮转到 .1 等），kind=model：

- model_ms 是整轮总耗时，model_stage_ms 是当前/失败阶段耗时。
- mcp_inherited_servers 是创建线程前发现的服务数；mcp_servers 是该线程核对时的服务数。
- mcp_catalog_tools 是目录条目数，可能含缓存，不等于可调用数量。
- mcp_disabled_servers 是明确禁用的服务数；mcp_unverified_servers 是未能证明禁用的服务数。
- mcp_new_servers 是建线程后发现、初始禁用清单中没有的服务数，用来识别目录在初始化期间发生变化的可能性。
- reasoning_effort 是协议返回的推理强度。诊断不保存服务/工具名称、地址、凭据、字幕、prompt 或回复正文。

若工具错误的 mcp_unverified_servers 大于 0，说明本轮未通过禁用核对；若 mcp_new_servers 也大于 0，可进一步检查启动期间的目录变化。若超时发生在 generation，说明轮次已提交，需查模型生成/服务响应；若发生在 mcp_discovery 或 mcp_verify，优先查工具状态接口响应。旧日志缺少阶段字段，不能倒推这些细节。

新增自动化验证覆盖 disabled 配合非空缓存目录、connected 配合空目录、starting/未知状态、新服务，以及工具检查、提交轮次和生成阶段的超时区别；同时验证元数据传给消费者诊断时剔除正文和任意字符串。真实会中下一次故障仍需要带新增字段的记录来确认。

完整 147 项产品测试通过；修正后的真实订阅合成调用通过。仍未复现用户那次工具错误，不将潜在误报修正表述为真实故障根因已确认。
