# v2 飞书会议会参谋

产品使用飞书官方会议字幕，持续采集并保存带说话人的输入，通过 **Codex + ChatGPT 订阅，或 OpenCode + GLM-5.3** 自主判断发言、沉默和自然语言控制意图，回答一次完整发送到会议内。产品代码在 `src/feishu_meeting_adviser/`，旧实验在 [reference/spike](reference/spike/README.md)，运行时互不依赖。

TODO 2.5 已实现：显式绑定日程实例，consume 启动时读取一次日程描述和关联 Docx / Wiki 中的 Docx，作为带来源、版本和省略标记的会议背景。材料不会自行触发回答，更新后重启 consume 才重新读取。用户 OAuth 只用于材料读取，VC 入会、字幕和会中发送继续使用应用身份。操作见 [USER_OAUTH](docs/USER_OAUTH.md)、[只读探测](docs/CONTEXT_PROBE.md) 和 [2.5 验证记录](docs/STAGE2_CALENDAR_VALIDATION.md)。

按用户决定，测试随各 TODO 项进行，不单列 2.3，2.4 已确认完成。Word / PDF 为后续 2.6，参会者控制及权限暂移至 2.7；输入源的跨会议回归 1.9 / G1 及 20 秒 P95 仍待验证。见 [TODO](docs/TODO.md) 和 [阶段 2 验证](docs/STAGE2_VALIDATION.md)。

## 安装与配置

Python 3.11+，本机 POSIX 文件系统。模型后端选择与切换见下文；OpenCode 设置及实测见 [OPENCODE_GLM](docs/OPENCODE_GLM.md)。进入 `v2/`，使用已有虚拟环境安装产品：

```bash
python3 -m pip install -e .
codex login status
```

若未安装依赖或需要新环境，可先执行 `python3 -m venv .venv` 和 `source .venv/bin/activate`。模型接入已在 Codex CLI `0.162.0` 核对；需要 `codex` 在 PATH 中且已用 ChatGPT 账号登录。产品检查订阅认证，不会自动改用 API key。

仅在还没有本机配置时复制 [config.example.yaml](config.example.yaml) 为 `config.yaml`，填写应用凭据、9 位会议号及密码，并执行 `chmod 600 config.yaml`。已有配置不要覆盖。可以追加：

```yaml
llm:
  backend: codex
```

这样 `join` 成功后发送介绍，`consume` 默认采用真实模型。旧配置没有 `llm` 时仍默认固定测试规则；用 `consume --backend codex` 可单次覆盖，首次模型消费补发介绍。

默认沿用 Codex 当前模型；本机合成调用使用 `gpt-6-astra`。可设置 `llm.model`；`llm.role_file` 为空时加载包内 [meeting_adviser.md](src/feishu_meeting_adviser/meeting_adviser.md)，也可指定本机 Markdown 路径。配置或角色文件更改后需重启消费者，新角色会重新检查待发候选。

默认批次等待 2 秒、模型上下文预算 30000 字符（含角色、材料、字幕及证据）、有效发言时间窗口 120 秒、两次普通回复至少间隔 10 秒、模型整次调用超时 90 秒。其余参数见配置示例。每次决定使用有界上下文，不承诺完整长会议记忆；超过窗口的资料仍保存在本机供核对。

## 使用 OpenCode + GLM-5.3

本机已有 OpenCode 登录时，可以在终端 B 直接切换：

```bash
.venv/bin/python -m feishu_meeting_adviser consume --backend opencode --watch
```

先停止旧 consume，同一数据目录不同时运行两个模型消费者。终端 A 的 collect 可以继续运行。默认模型为 `zai-coding-plan/glm-5.3`，不自动回退到其他模型或 Codex。希望 join / consume 默认都使用 OpenCode 时，在已有配置的 llm 块中设置：

```yaml
llm:
  backend: opencode
  opencode_executable: opencode
  opencode_model: zai-coding-plan/glm-5.3
```

`llm.executable / llm.model` 继续仅用于 Codex；两组后端参数独立。角色文件、日程绑定、字幕消费、任务恢复和回复发送逻辑共用。GLM 使用 OpenCode 本机已登录的对应 provider；缺少登录时先用 `opencode auth login`，选择 Z.AI Coding Plan。凭据留在本机，不填入会议 YAML 或聊天。详细保存、超时和验证边界见 [OPENCODE_GLM](docs/OPENCODE_GLM.md)。

## 绑定日程材料

先按 [USER_OAUTH](docs/USER_OAUTH.md) 完成本机授权，再用 [context_probe](docs/CONTEXT_PROBE.md) 核对目标日历和完整日程实例 ID。在已有本机配置中追加以下块，使用核对后的 ID：

```yaml
meeting_context:
  calendar_id: "目标 calendar_id"
  event_id: "本次日程实例 event_id"
  identity: user
```

两个 ID 都留空或省略整个块时关闭材料读取；仅填一个会报配置错误。应用可读目标资料时也可选择 application，失败不会自动换身份。会话的会议链接须与日程一致，入会时间默认须在日程开始前一小时到结束后一小时内；重复日程必须绑定本次实例。下一场会议同时修改会议号和日程绑定，不能沿用旧日期的日程实例。

启动 consume 后，终端显示描述、引用/附件清单、Wiki 类型及正文预览，同时保存至私有 `data/content_previews/<会议标识hash>/materials.jsonl`。每段最多 2000 字符，保留原长度和截断标记；模型文本另受材料预算（默认 10000 字符）和总上下文预算约束，优先保留近期字幕。实际测试的在线文档链接来自 `description_rich`，程序同时核对普通 description。只读取日程明确引用，不递归打开文档中的链接。

材料状态为 ready / partial / unavailable；未支持类型和超预算部分明确标为 partial。应支持的资料读取失败会使 consume 退出并阻止新回答，collect 继续独立采集。修复权限或绑定后重启 consume，不复用失败前的旧缓存；更换授权账号也需重启。每次启动重新读取，版本变化后重新检查待发候选，材料变化不重答历史问题。完整规则和保存边界见 [方案](docs/STAGE2_CALENDAR_CONTEXT_PLAN.md) 与 [资料操作](docs/DATA_OPERATIONS.md)。

## 两个终端运行

两个终端都在同一台机器、同一个 `v2/` 目录和虚拟环境中。参会者无需运行代码。主持人在采集和模型处理前口头说明 bot 的作用及字幕保存和模型传输。

终端 A：

```bash
python3 -m feishu_meeting_adviser join
python3 -m feishu_meeting_adviser collect --show-text
```

`collect` 自行持续轮询，默认每轮完成后等待 0.7 秒，HTTP 请求最短间隔同为 0.7 秒；实际轮询周期还包括请求与处理耗时。`--show-text` 实时打印新入库字幕、说话人和时间，也打印修订/冲突候选。终端正文是额外的本机副本，按会议授权保管。

终端 B：

```bash
python3 -m feishu_meeting_adviser consume --backend codex --watch
```

`consume` 持续保存输入、调用模型并发送完整回答；模型慢或失败时 `collect` 仍继续入库。省略 `--watch` 则处理当前一批可决策输入后退出。不需要固定唤醒词或特殊指令，参与规则由角色文档描述。

参会者自然语言控制及权限规则已移至 TODO 2.7，当前不作为默认角色的功能承诺。底层控制状态实现保留；完整停止使用下方终端操作。模型只返回结构化决定，发送和状态校验由产品执行。

完整停止：先在终端 B 按 Ctrl-C 停止模型消费，再在终端 A 按 Ctrl-C 停止采集；机器人仍在会中。如需离会再执行：

```bash
python3 -m feishu_meeting_adviser leave
```

重启先启动 `collect`，再启动 `consume`；输入、任务、暂停状态和发送记录从本机数据库恢复。同一数据目录只允许一个采集进程和一个模型消费者。采集的定期清理与模型消费共用维护锁，遇到短暂占用会等待，Ctrl-C 可中断等待。

## 本地 LLM harness

无需创建会议或真人发言，可以用独立的 `meeting_adviser_harness` 包测试角色、会议描述、在线文档背景和多人对话。它持续接收本地字幕，后台直接运行产品的 `AdviserRunner`；上下文预算、批处理、模型调用、证据检查、过期和发送前复核与正式 consume 共用，只有日程、字幕和发送三个外部适配器改为本地文件、终端输入和终端输出。

```bash
python3 -m meeting_adviser_harness \
  --description-file ./description.md \
  --role-file ./meeting_adviser.md \
  --backend opencode
```

输入 `张三: 第一段发言`、`李四: 补充意见` 模拟多人；也可用 `/speaker 张三` 后连续输入。`/context` 显示最近一次实际模型输入，`/reload` 重新读取角色、描述及其文档但保留本轮字幕，`/reset` 开始新的模拟会议，`/quit` 退出。模型运行时仍可继续输入，新字幕会按正式产品规则影响待发送回答。完整说明和 JSONL 回放格式见 [HARNESS](docs/HARNESS.md)。

交互终端只显示最终允许回复、显式命令结果和错误；模型保持沉默时不输出，后台状态、材料预览和诊断也不会打断输入。完整 JSON 事件写入启动时显示的私有轮转日志。需要实时查看全部事件时，另开终端运行 `python3 -m meeting_adviser_harness watch`。

## 查看状态与处理异常

```bash
python3 -m feishu_meeting_adviser status --consumer adviser
python3 -m feishu_meeting_adviser replies
python3 -m feishu_meeting_adviser replies --show-text
```

`status` 的 `pending` 是源尚未交付给所选消费者的数量，`adviser` 字段是模型任务、active/paused 和回复队列状态。源 `pending: 0` 只表示输入已可靠交给模型消费者，不代表模型已回答或消息已可见。

模型错误在状态中显示固定分类，持续模式低频恢复，不把失败输出当作回复。 `consume --watch` 新增 `model_stage` 显示当前或最近一次调用阶段；`mcp_verify` 是工具禁用核对，`turn_start` 是提交模型轮次，`generation` 是等待模型决定。详细耗时和工具状态计数保存于 `data/diagnostics/diagnostics.jsonl`，诊断步骤见 [MODEL_DIAGNOSTICS](docs/MODEL_DIAGNOSTICS.md)。若收到 `uncertain` 或 `rejected` 发送记录，停止 `consume`，在会议内核对，再处理对应 UUID：

```bash
python3 -m feishu_meeting_adviser resolve-reply --uuid <UUID> --outcome sent
# 确认没有发出且仍需要重试时，保留 UUID 并让模型重查当前讨论。
python3 -m feishu_meeting_adviser resolve-reply --uuid <UUID> --outcome retry
# 放弃待核对的发送，不撤回已经显示的消息。
python3 -m feishu_meeting_adviser resolve-reply --uuid <UUID> --outcome cancel
```

介绍发送未确认时暂停模型处理，核对后再恢复；同一次入会不会重复介绍，离会后重新入会会重新介绍。默认不自动重发不确定消息，避免重复发言。

下一场会议先停止旧 collect 与 consume，修改配置中的会议号，再运行 `join`，无需例行 reset-session。`join` 会用已有字幕接口核对旧 bot：确认已不在会中则重新入会；同一会议仍可用则复用身份，不重复介绍；如果 bot 仍在另一场会议中，join 提示先显式运行 `leave`，再运行 `join`；join 不会替你离开另一场会议。9 位会议号重复使用时，旧 bot 已离会会取得新的内部会议 ID 和入会身份。

`leave` 返回 121104 时也会核对 bot 是否已不在会中，能确认则自动完成会话清理。权限/网络错误、缺少内部 ID 的不确定入会或无法证实 bot 已离会时保留状态并报错；reset-session 仅保留为人工核对后的异常恢复入口。自动恢复不删除旧会议字幕和消费资料。

修订冲突通过 `conflicts [--show-text]` 检查，确认候选后用 `resolve-conflict --delivery-id <交付哈希>` 接受。模型不能把未解决冲突当作确定事实。

## 资料与测试

字幕、上下文、任务和回复保存在私有 `data/`，诊断不记录正文或凭据。Codex 使用 ephemeral 线程；OpenCode 使用独立临时服务/会话并在每次调用后清理。模型外部工具、MCP 及继承插件被禁用；恢复以产品数据库为准。停止两个进程后可用 `delete-meeting-data --confirm` 删除当前会议的托管资料，bot 控制会话保留以便离会。保存期限、预算、清理边界及删除说明见 [DATA_OPERATIONS](docs/DATA_OPERATIONS.md)。

固定链路规则显式使用 `consume --backend rule`：收到“项目截止日期是明天”时发送“{speaker}，项目截止日期是今天。”；它只用于测试，不与模型消费者同时运行。正式模型依据实际上下文，不沿用这条固定更正。

安装产品后运行测试：

```bash
python3 -m unittest discover -s tests -v
```

自动化测试使用合成输入、假模型与假飞书，不调用真实会议。harness 可调用所选真实模型和已授权的飞书文档，但不会加入会议或发送飞书消息。已另行验证真实订阅的沉默、暂停/恢复和合成问题回答；实际会中展示和用户对话脚本验证随相应功能项进行，20 秒 P95 仍为后续优化目标。
