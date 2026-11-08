# 远程 LLM 后端调研与设计参考

日期:2026-11-08。背景:`consume` 当前通过 `OpenCodeBackend` 在本机拉起 OpenCode 子进程调用 GLM(`zai-coding-plan/glm-5.3`)。本文记录一次讨论的结论,覆盖四个问题:① opencode 能否远程;② 多用户会话隔离;③ Coding Plan 包月与直连 API 的取舍;④ 未来引入工具/MCP 时的路线选择。**全部为调研与设计结论,未实施;函数名以当期实现为准。**

## 结论速览

| 问题 | 结论 |
| --- | --- |
| 当前代码能否直接指向远程 opencode | 不能。进程生命周期、回环绑定、stdout 地址发现、每决策新实例均与本地子进程耦合,需改造 |
| 单个远程 opencode 实例能否隔离多用户 | 不能。单实例是单租户的(共享认证/存储/配置/凭据),隔离单位是进程;须每用户或每决策一实例 |
| 包月 Coding Plan 能否直连 API | 条款上限定 coding 工具使用,专端点物理可调但违约有封禁风险;opencode 桥是合规路径(条款细节未联网复核) |
| 直连按量 API 后是否需要自建 skill/context/memory | 不需要。当前任务单轮无状态,这些机制在现配置中本就全部关闭;只需薄 HTTP 壳 |
| 引入工具/MCP 后是否继续用 agent runtime | 是,继续用 opencode:工具管线已铺好(`tools` 参数、`mcp` 槽位、权限规则),但属于安全模型转向 |

## 1. 现状:为什么当前代码只能本地

`OpenCodeBackend`(`advisor/src/feishu_meeting_adviser/opencode_backend.py`)与本地子进程的耦合点:

- **自起进程**:`_start()` 经 `opencode_guard` 用 `subprocess.Popen` 启动 `opencode serve --pure --hostname 127.0.0.1 --port 0`,强制回环、随机端口;配置面只有 `opencode_executable`(本地可执行文件路径),没有 server URL 入口。
- **stdout 地址发现**:从进程输出用正则 `http://127\.0\.0\.1:([0-9]{1,5})` 解析 origin,远程服务没有这个输出。
- **每决策一实例**:每次决定随机密码(`secrets.token_urlsafe(32)`)、私有临时 runtime 目录、`OPENCODE_AUTH_CONTENT` 内存传凭据、`OPENCODE_CONFIG_CONTENT` 注入 deny-all 配置——隔离全靠控制子进程环境变量实现;决策后 `DELETE /session`、杀进程、清目录。
- **生命周期管控**:guardian 监听父管道 EOF、超时看门狗关 stdin 逼停慢速滴流、进程组 SIGKILL(`close()`)。这些只对本地子进程有效。

启动后的交互本身是普通 HTTP JSON API(health、`/config`、`/agent`、`/mcp`、`/provider`、`/experimental/tool/ids`、`/session/*/prompt_async`、消息轮询、删会话),且 `_verify()` 本来就通过 HTTP 复核权限与工具配置——这是远程化可行的技术基础。

## 2. 远程化的改造点与安全降级

改造本身不难:新增配置(如 `llm.opencode_server_url` + 服务端凭据),`_start()` 跳过进程管理,`self.origin` 直接指向远程地址;stdout 解析、guardian、看门狗杀进程、runtime 目录清理按远程模式跳过。但要接受安全模型的降级:

| 保证 | 本地(现状) | 远程 |
| --- | --- | --- |
| deny-all 配置 | 自己注入,构造性保证 | 只能靠 `_verify()` 事后核验,需信任远端 |
| 每次决策隔离 | 全新进程+随机密码+私有目录 | 共享服务器,会话状态隔离变弱 |
| 会议转写数据 | 不出本机 | 离开本机,隐私需评估 |
| 超时/卡死处理 | EOF+kill 精确逼停 | 只剩 HTTP 超时 |
| 凭据 | 内存传递、用完即焚 | 常驻远端 |

## 3. 多用户隔离

单个 `opencode serve` 进程没有任何"用户"概念,关键面全部进程级共享:

| 隔离维度 | 实际情况 | 影响 |
| --- | --- | --- |
| 认证 | 一组 Basic auth(进程级环境变量) | 服务器无法区分用户,所有人同一凭据 |
| 会话存储 | 所有 session/message 落同一 XDG data 目录,`GET /session` 列出全部会话 | 用户 A 可读用户 B 的转写与决策 |
| 配置/权限 | config、agent、deny-all、MCP 均进程级 | 无法按用户下发策略 |
| 模型凭据 | provider key 进程级共享 | 无法按用户计费/审计 |

会话 ID 不可猜不构成隔离边界(列表接口直接暴露)。可行方案:

1. **每用户(或每次决策)一实例——推荐。** 把本地 guardian 模式搬到远端:认证网关(反代)做用户认证与路由,supervisor 复刻 `opencode_guard` 逻辑(EOF 即杀、临时目录清理),每请求/每用户 spawn `opencode serve --pure --port 0`(随机密码+私有 XDG+内存注入配置)。完整保留现有隔离模型;代价是远端承担进程调度。
2. **直连 provider API。** 若动机只是计算/凭据集中,且已具备按量 API key,多用户隔离退化为标准的 key-per-user 问题。见第 4 节约束。

**不成立的第三条路**:多用户共享一个远程实例、靠 session ID 区分——安全幻觉,上表每一行都会踩。

## 4. Coding Plan 约束与直连 API 的取舍

**包月 Coding Plan 的性质**(未联网复核,以 Z.AI 官方文档为准;仓库内依据见 [OPENCODE_GLM](OPENCODE_GLM.md)):

- GLM Coding Plan 是订阅制专用通道,走专用 coding 端点(OpenAI/Anthropic 兼容协议),与按量计费的开放平台 API 是两套计费体系;条款限定在 coding tools 中使用。
- 端点物理上可以裸调,但违反套餐条款,有检测/限流/封禁风险,且检测策略可被单方面变更,不是稳定的工程依赖。
- 用 opencode 做桥是正解:调用发生在真实 coding agent 内部,包月额度名正言顺。此为行业通例(如 Anthropic 订阅与 API 分离)。

**若改用按量 API 直连,不需要自建 skill/context/memory 机制**,因为当前配置已把这些全部关闭(`mcp:{}`、`instructions:[]`、`plugin:[]`、`compaction` 关闭、`DISABLE_EXTERNAL_SKILLS`、tools 全 false、每决策新会话即删)。今日 opencode 对本项目只剩三个作用:凭据路由到 coding 端点、协议处理、一个必须反复验证"关干净了"的沙箱。任务是单轮无状态:snapshot 进 → 一个 JSON 决策出,校验靠本地 `Decision.parse`。直连只需薄壳:

- 一个 HTTP 调用(system=角色文件,user=snapshot);
- 超时/重试/错误映射到现有 `ModelError` 语义(逻辑已在 `_request`/`_result`,平移即可);
- JSON 输出校验复用 `Decision.parse`;
- token 计量(诊断用,API 响应自带 usage)。

额外收获:现有 guardian、`_verify()` 逐工具 deny 核对、运行目录隔离的存在原因是 opencode **有**工具面需要封死;裸 API 调用不存在工具面,"材料是数据不是指令"从"靠验证"变为构造性成立,安全模型更干净。**只有任务演化为 agentic(模型自己翻文档、多轮推敲、跨会议记忆)时才需要 agent 框架能力**,那时自建等于重造 opencode。

## 5. 工具/MCP 演进(与远程化正交)

需要调用工具或 MCP 查信息后,继续用 opencode(而非自建 agent 循环或换 runtime):凭据路由已解决;`prompt_async` 已在传 `tools` 映射(现全 false,翻 true 即放行);注入配置已有 `mcp` 槽位;权限规则体系已在 `_verify()` 中逐条核对过。换其他 runtime 会失去包月优势。

但这是**安全模型的转向**,不是开开关,需要:

- `_verify()` 反转语义:核验目录里只有预期工具/MCP,且每个都是声明的权限(read-only 放行,其余 deny);
- `_result()` 放行 tool/file/patch 部件,追踪工具调用生命周期,终态仍强制单一 JSON;
- agent `steps`(现 2)按查找深度调大,timeout(现 90 秒)重预算;工具结果撑上下文,`compaction` 策略重审;
- `_decide_once` 的单 user turn 断言适配工具循环产生的中间消息;
- **prompt injection 成为真实攻击面**:转写内容现被当数据,因模型无手而安全;有工具后恶意会议内容可诱导工具调用。防御降级为白名单+语义边界:只放行只读无副作用工具,write/bash/提交类 deny;工具来源证据的合法性校验;每决策一实例隔离更须坚持(MCP 子进程扩大清理面);
- 更新 [OPENCODE_GLM](OPENCODE_GLM.md) 中"不放开工具作为兼容措施"的不变量表述。

推进顺序:明确要查什么/经哪个工具/只读还是可写 → 改 `_verify`/`_result`/steps/timeout → 注入 MCP 声明与逐工具 permission → 补"转写内容诱导工具调用"对抗测试 → 更新不变量文档。

## 6. 实现时的入口清单

| 位置 | 作用 |
| --- | --- |
| `opencode_backend.py` `_start()` | 进程拉起/origin 发现,远程模式改造主入口 |
| `opencode_backend.py` `_environment()` | 隔离环境注入(凭据/配置/XDG),远程 supervisor 复刻对象 |
| `opencode_backend.py` `_verify()` | 权限/工具核验,远程与工具化时语义均需调整 |
| `opencode_backend.py` `_result()` / `_decide_once()` | 输出校验与轮询,工具化时需放行 tool parts |
| `opencode_guard.py` | EOF 即杀守护,远端 supervisor 的复刻蓝本 |
| `config.py` llm 节 | 新增远程/工具相关配置项的位置 |
| [OPENCODE_GLM](OPENCODE_GLM.md) | 现行隔离不变量与验证记录,改动后须同步 |
