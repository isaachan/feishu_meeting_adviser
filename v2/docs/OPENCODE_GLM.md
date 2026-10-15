# OpenCode + GLM-5.3 后端

日期：2026-10-15。用户要求保存当前修改后新增此后端，保留 Codex。前一批 OAuth / 2.5 / 工具隔离 / 用户角色修改已提交为 dedb86b。真实会中 Codex 的四次超时均在 generation，整次约 90 秒且工具隔离已通过，详见 [MODEL_DIAGNOSTICS](MODEL_DIAGNOSTICS.md)。此次没有把上限调长来替代后端接入，也不将超时归为日程读取失败。

## 配置和运行

已在 OpenCode 1.18.35 核对本机 API 和可用模型。默认精确选择 `zai-coding-plan/glm-5.3`，不自动换模型或后端。

在本机配置原有 llm 块中设置，保留 role_file、超时、预算等其他字段：

```yaml
llm:
  backend: opencode
  opencode_executable: opencode
  opencode_model: zai-coding-plan/glm-5.3
  timeout_seconds: 90
```

Codex 的 executable / model 字段继续保留，OpenCode 只使用 opencode_executable / opencode_model。CLI 的 `consume --backend opencode` 可临时覆盖 backend，不会丢失另一后端的设置。

凭据复用 OpenCode 本机对应 provider 的 API 登录（默认 `~/.local/share/opencode/auth.json`，遵循原 XDG_DATA_HOME），或继承的 provider 环境凭据。未登录时执行 `opencode auth login` 并选择 Z.AI Coding Plan；产品不会在聊天、命令行参数或会议 YAML 中请求/保存 key。此实现的独立 API 凭据读取路径不支持把其他 provider 的 OAuth 登录当作 GLM 凭据。

终端 A 沿用 join / collect；终端 B 先停止旧 consume，再执行：

```bash
cd /Users/kai.han/code/feishu_meeting_adviser/v2
.venv/bin/python -m feishu_meeting_adviser consume --backend opencode --watch
```

材料、角色更新仍需重启 consume；无需重新授权飞书 OAuth。修改配置文件前停止两个进程，避免既有配置变更保护中止采集。

## 调用与恢复

- 产品自己启动带随机本机认证的 loopback HTTP 服务，每次决定使用独立临时目录和新会话。每次读取配置、指定 agent 的有效权限、MCP、provider/model 可用性及工具目录，检查失败发生在提交会议正文之前。
- 隔离子进程 XDG 配置/数据/缓存/状态、关闭继承插件、外部 skill/Claude 指令、项目配置、共享、更新及快照；只把所选 API 登录通过 OPENCODE_AUTH_CONTENT 传入进程内存，不复制全局 auth/config 文件。LLM 不取得会议或应用凭据。
- 角色文件通过 system 传入，字幕和带来源材料作为快照文本；自定义 meeting-decision agent 只输出 JSON。所有模型工具的有效权限和本次调用工具开关均为 deny/false，遇到 tool/file/patch/subtask 等输出立即拒绝。
- 本机 1.18.35 对显式 format 对象出现读回错误：message/list 返回 HTTP 400，指向存储消息中的 OutputFormatJsonSchema；显式 text 格式亦失败。使用默认文本格式，要求无代码围栏的单一 JSON，完整响应再由现有 Decision.parse 严格验证字段、类型、动作、长度和证据形状；语义证据、新鲜度和发送检查继续由 AdviserStore 执行。不会从非法输出中提取一段文字发送，也不通过 StructuredOutput 工具绕开 deny。
- prompt_async 后只读取本次新会话的消息，关联 OpenCode 分配的 user message ID，核对 assistant 的 session / parent / provider / model，完整完成后才保存决定。没有完成、被截断、API 错误、工具请求、多个决定或无效 JSON 均不发送。
- 原整次期限覆盖启动、检查、提交、生成和会话清理；低频恢复继续由既有消费者调度。截止/停止监视会关闭服务，即使 HTTP 正文慢速滴流也能中断。POST 不自动重放；进程被直接 SIGKILL 后，守护进程通过父管道 EOF 结束所拥有的服务。模型重算仍可能发生，不承诺一次性推理；回复持久化、固定 UUID 和不确定发送处理沿用既有实现。

## 保存与删除

独立临时数据位于 `data/model_runtime/opencode/<会议标识hash>/run-*`。目录私有，守护进程 umask 077；OpenCode 的数据库、日志和缓存都在该运行目录。每次成功或失败后结束服务并删除临时目录，成功路径还显式删除本次会话。父进程强制退出时守护进程负责终止与清理；双方同时被杀或清理失败的残留在后续启动/会议删除时按运行锁清理，活跃守护进程保护其目录。

显式 delete-meeting-data 与 tombstone 维护覆盖该会议的残留运行目录；活跃守护进程尚在退出时，显式删除报错并可重试。共享 OpenCode 登录不删除。会话清理不能删除供应商侧数据，也不撤回已发会议消息。脱敏诊断只新增 backend、阶段、轮询数及输入/输出/推理 token 计数，不保存 provider 错误正文、prompt、回答、模型私有路径或认证信息。

## 验证记录

新增 9 项 OpenCode 测试，覆盖精确模型/角色/材料输入、严格输出、权限/登录/模型缺失时阻止提交、整次期限和慢速 HTTP、中断、真实子进程 SIGKILL 清理、登录/配置隔离、残留清理、默认配置与诊断。另有 CLI 后端路由、带材料的 HTTP 决策至持久化假发送及删除测试。完整产品测试共 201 项，200 项通过、1 项角色控制文案按用户要求跳过。

真实调用使用现有 Z.AI Coding Plan API 登录：

| 场景 | 结果 |
| --- | --- |
| 合成空会议 | 返回沉默，整次约 6.31 秒，输入 368 / 输出 26 / 推理 92 tokens，临时目录无残留 |
| 已授权日程材料 + 合成新鲜提问 + 假飞书 | material_state=ready，1 篇文档、88 字正文进入快照；真实 GLM 决定 1 次、假发送 1 条，回复包含文档标题，重跑不重复；整条验证约 23.32 秒（含材料读取和进程启动） |

第二项使用用户当前角色文件、真实用户材料客户端和明确日程绑定，但会议内部 ID、入会时间、字幕及发送端为模拟；没有向真实会议发消息，临时验证正文未提交。不能将单次时长当作真人说完至会中可见的 P95，也不据此保证所有问题均不会超时。真实会中展示和持续运行仍需用户复测。

## 官方依据

- [OpenCode Server](https://opencode.ai/docs/server/)：loopback serve、Basic 认证、会话/消息/删除接口。
- [OpenCode SDK](https://opencode.ai/docs/sdk/)：结构化接口及错误契约；本机最终以服务生成的 /doc 规格核对。
- [OpenCode Config](https://opencode.ai/docs/config/) 与 [Permissions](https://opencode.ai/docs/permissions/)：合并优先级、agent 权限覆盖及 deny 语义。
- [Providers](https://opencode.ai/docs/providers/)：本机 auth 保存及 Z.AI Coding Plan；精确 GLM-5.3 ID 另由本机 models / provider API 实测确认。

当前针对已核对的 1.18.35 接口实现；未来版本字段/权限语义不符合核对要求时显式失败，不放开工具作为兼容措施。
