# 飞书富文本回答设计（reply-md-v1）

状态：已实现（2026-11-22 立项并落地）。实现要点：gateway 接受 `decision-v1|v2`
（`llm-gateway/src/llm_gateway/service.py`）；advisor `--decision-output` 选择版本，
outbox `payload.format` 标记 `reply`（`advisor/src/advisor/api.py`）；bot
`--rich-text`/`BOT_RICH_TEXT` 开启卡片渲染，`bot/src/bot/render.py` 做方言清洗与
纯文本回退。灰度顺序见 §9 与 [DEPLOYMENT](DEPLOYMENT.md) §5。

## 1. 背景

当前 LLM 回答经 outbox 以纯文本下发：`advisor` 把 `decision.reply` 原样写入 outbox
（`advisor/src/advisor/store.py` 的 `save_reply`），`GET /outbox/pending` 包装为
`payload={"text": ...}`（`advisor/src/advisor/api.py` 的 `pending_outbox`），bot 用
`msg_type="text"` 发送（`bot/src/bot/lark_bot.py` 的 `send_dm` / `send_reply`）。

飞书支持富文本消息。会议建议通常包含结论、要点列表和链接，纯文本可读性差。目标是在
不破坏现有决策/投递契约的前提下，让回答以富文本呈现。

## 2. 目标与非目标

目标：

- LLM 回答支持加粗、行内代码、有序/无序列表、HTTPS 链接。
- 渲染失败时自动回退纯文本，不丢消息。
- advisor / llm-gateway 保持与飞书无关，admin-site 等其它消费端可复用同一份内容。
- 分批上线、可回滚。

非目标：

- 本期不做表格、图片、投票、按钮、@ 提及、流式更新。
- 不改变决策证据校验、会话状态机和订阅语义。
- 不让 LLM 直接输出飞书 `post` / `interactive` JSON。

## 3. 架构决策

1. **内容与展示分离**：`reply` 只承载语义内容（受限 Markdown），飞书渲染只在 bot 做。
2. **复用现有字符串字段**：Markdown 是纯文本的超集，不新增 `reply_blocks` 等结构化内容模型，
   只升级 `reply` 的语义版本。
3. **单点渲染**：渲染收敛在 `ImTransport` 边界，不在各个 `_cmd_*` / `_reply` 调用点分别处理。

不采用的方案：

- **advisor/gateway 直接产出飞书 JSON**：把渠道细节带进云端核心，且 gateway 与 advisor
  当前完全不感知飞书；outbox 还被 admin-site 时间线消费。
- **新增 `reply_blocks` 结构化数组**：需要同时改 gateway 校验、advisor 校验、prompt、契约和
  渲染器，成本高；当前目标只是可读性。

## 4. 契约变更

### 4.1 推理输出：`decision-v1` → `decision-v2`

字段集合保持不变，仅改变 `reply` 的语义：

```json
{
  "control": "none",
  "control_sentence_keys": [],
  "reply": "**结论**：建议先补预算数据。\n\n- 数据口径沿用 Q3\n- 参考 [预算模板](https://example.com/budget)",
  "reply_sentence_keys": ["S12"]
}
```

- `output="decision-v2"` 表示 `reply` 遵循本文 §5 的 `reply-md-v1` 方言。
- 校验器结构不变：`llm-gateway/src/llm_gateway/decisions.py` 与
  `advisor/src/advisor/model_backend.py` 仍校验四个字段、证据键和长度。
- gateway 同时接受 `decision-v1` 与 `decision-v2`；advisor 默认请求 `decision-v2`，
  可用 `--decision-output decision-v1` 回滚。`output` 参与 gateway 的 content hash
  （`llm-gateway/src/llm_gateway/service.py` 的 `canonical_hash`）与
  `advisor` 的 `remote_identity`，版本切换会自然改变幂等键，属于预期行为。
- prompt 更新（`llm-gateway/src/llm_gateway/backends/opencode.py` 的 `AGENT_PROMPT`）：
  「外层只返回 JSON，不要 Markdown 代码块；`reply` 字段内使用受限 Markdown」。

### 4.2 outbox payload：增加 `format`

`GET /outbox/pending` 对 `kind=reply` 的行返回：

```json
{"payload": {"text": "...markdown...", "format": "markdown"}}
```

- `subscription_notice`、审批提示等 bot 本地文案保持 `"format": "text"`，避免
  `回复「同意 #7」` 中的 `#` 被 Markdown 解释。
- 这只是 payload 的增量字段，消费端忽略也不影响；outbox 表结构不变，仍存原始内容。
- bot 的兼容读取：`payload.get("format", "text")`。

### 4.3 bot 传输接口

把 `ImTransport.send_dm(open_id, text)` / `send_reply(message_id, text)` 改为接收消息对象：

```python
@dataclass(frozen=True)
class OutboundMessage:
    text: str
    format: str = "text"   # text | markdown
```

bot 内部调用点（`bot/src/bot/service.py` 的指令回执、`poll_once` 投递）统一构造
`OutboundMessage`。渲染与消息类型选择只发生在 `LarkTransport`，后续新增渠道时无需改业务层。

## 5. reply-md-v1 方言

允许：

- 加粗 `**文本**`
- 行内代码 `` `code` ``
- 无序列表 `- item`
- 有序列表 `1. item`
- 链接 `[文本](https://...)`
- 段落空行、单换行

禁止：

- 表格、图片、HTML、脚注、多层嵌套列表
- `@` 提及、`<at>` 标签
- 非 `https` / `http` 链接
- 代码围栏（除非后续明确需要多行代码）
- 行首单独使用 `#`（避免与「#7」这类业务编号冲突；标题用加粗表达）

约束：

- 首行给出结论，正文控制在短列表内，避免大段排版。
- 现有 4000 字节上限继续作用于原始 `reply`；Markdown 标记和渲染后的飞书消息体
  另行校验（见 §7）。

## 6. 飞书渲染

新增 `bot/src/bot/render.py`，接口形如：

```python
def render(message: OutboundMessage) -> tuple[str, dict]:
    """Return (msg_type, content) for Feishu. Falls back to ('text', {...})."""
```

两种可选策略：

| 策略 | 优点 | 成本 |
| --- | --- | --- |
| `interactive` 卡片 + Markdown 元素 | 直接消费 Markdown 字符串；后续可 PATCH 更新卡片、加「同意/拒绝」按钮 | 视觉上是卡片而非普通气泡 |
| `post` 富文本 | 聊天气泡观感原生 | 需自建 Markdown→tag 解析，加粗/链接/列表都要逐段转换 |

建议首期用 `interactive` 卡片内单个 Markdown 元素，实现简单且为将来的卡片交互留出空间；
把渲染策略封装成可替换函数，若产品更偏好气泡观感再补 `post` 策略。实现前按飞书官方文档
复核当前租户可用的消息类型和 Markdown 子集。

回退规则：

- 渲染器本地校验失败（非法方言、超长、危险链接）→ 直接按纯文本发送。
- 飞书 API 因内容格式拒绝（确定未投递）→ 记录原因后用纯文本重试一次。
- 网络超时/未知结果 → 不重试，沿用现有 `uncertain` 语义，避免重复消息。

## 7. 关键约束与风险

- **幂等**：outbox `message_uuid`（`advisor/src/advisor/decision.py`）与飞书 reply 的
  `uuid`（`bot/src/bot/lark_bot.py`）必须基于原始 `reply` 计算，不能基于渲染后的 JSON。
- **长度**：现有 4000 字节是纯文本预算；Markdown 标记和卡片包装都会膨胀。渲染后需校验飞书
  消息体大小，超限回退纯文本，必要时在 prompt 中收紧回答长度。
- **安全**：
  - 只允许 `https`/`http` 链接，建议域名白名单（公司内网、NSDP、Jira 等）；不可信链接降级为纯文本。
  - 丢弃所有 `@` / `<at>` / 图片标签，防止模型输出 `@all`。
  - 用户可控文本（会议标题、姓名等）拼入回复时必须转义 Markdown 特殊字符。
- **本地文案**：`HELP`、`状态`、审批回执等由 bot 生成，默认 `format="text"`；如需富文本，
  逐条改造并测试，不能默认当成 Markdown。
- **admin-site 一致性**：admin-site 时间线展示同一份 outbox 内容，应使用同一方言渲染，
  否则会出现「bot 里好看、后台里是星号」的割裂。
- **卡片失败面**：卡片 JSON 结构错误会整条发送失败，因此渲染器必须做本地结构校验；
  纯文本回退是最后防线。

## 8. 测试与观测

- 单元测试：
  - `reply-md-v1` 解析/校验：允许语法、非法方言、超长、危险链接。
  - 渲染快照：加粗、列表、链接、行内代码、纯文本回退。
  - `pending_outbox` payload 含 `format`，`subscription_notice` 为 `text`。
  - `ImTransport` 契约测试：markdown → 卡片/文本两种路径。
- 集成测试：fake transport 断言实际发送的 `msg_type` 与 content；outbox 两阶段发送不回退重复。
- 观测：日志记录 `format`、渲染耗时、回退原因（`render_fallback`），便于灰度期统计。

## 9. 灰度与回滚

- bot 环境变量开关 `BOT_RICH_TEXT`（默认开启；`BOT_RICH_TEXT=0` 或 `--no-rich-text` 关闭），
  关闭时所有消息按纯文本发送。
- 上线顺序：gateway 支持 v2 → advisor 输出 `format` → bot 渲染开关打开 → advisor 请求 v2。
- 回滚：关 bot 开关即可回到纯文本；advisor 可回退请求 `decision-v1`，gateway 继续兼容。

## 10. 任务拆分（每项独立提交）

1. **D1 契约与协商**：本文件评审定稿；gateway 接受 `decision-v1|v2`，advisor 按配置选择
   输出版本；补契约测试。
2. **D2 prompt 与方言校验**：更新 `AGENT_PROMPT`，实现 `reply-md-v1` 轻量校验（或仅在 bot
   渲染层校验），测试模型输出样本。
3. **D3 advisor 投递格式**：`pending_outbox` 为 `kind=reply` 增加 `format`；digest/幂等键
   纳入方言版本；补单测。
4. **D4 bot 渲染层**：新增 `render.py`、`OutboundMessage`、`LarkTransport` 改造、纯文本回退、
   开关；补单测与渲染快照。
5. **D5 集成与灰度**：admin-site 复用渲染；集成床校验 markdown 投递；打开灰度并观察回退率。

## 11. 未决问题

- 首期渲染载体最终选 `interactive` 卡片还是 `post` 富文本（需真实客户端走查后定）。
- 链接是否启用域名白名单；白名单范围由安全/运维确认。
- 是否允许 LLM 使用多行代码块（当前会议建议场景判断为不需要）。
- 未来若需要表格或按钮，是否升级为 `reply_blocks` 结构化模型。
