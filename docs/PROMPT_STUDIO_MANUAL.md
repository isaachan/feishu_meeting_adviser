# Prompt Studio 使用手册

适用版本：`prompt-studio 0.1.0`（2026-11-27）
设计说明与任务拆分见 [PROMPT_STUDIO.md](PROMPT_STUDIO.md)。

本手册面向需要在本地或隔离服务器上调试会参谋提示词、模型与会议材料的研发同学。Prompt Studio 只做“回放 + 观察”，不接 bot、不投递 IM、不改生产链路。

## 1. 前置条件

- 仓库：`feishu_meeting_adviser`
- Python 3.11+
- 依赖模块：`auth`、`advisor`、`llm-gateway`、`prompt-studio`
- 运行 replay/serve：需要 `opencode`、OpenCode `auth.json`、真实 NSDP/Jira token
- 无模型自检：仓库自动化测试内部使用 fake backend，不需要模型
- 飞书文档抓取（可选）：`noam` + 个人飞书 web session

## 2. 安装

在仓库根目录执行：

```bash
python3 -m venv .venv-local
.venv-local/bin/pip install -U pip
.venv-local/bin/pip install \
  -e ./auth -e ./advisor -e ./llm-gateway -e './prompt-studio[dev]'

.venv-local/bin/prompt-studio --help
```

自检：

```bash
cd prompt-studio
../.venv-local/bin/ruff check src tests
../.venv-local/bin/pyright
../.venv-local/bin/python -m unittest discover -s tests -v
cd ..

.venv-local/bin/python -m unittest discover -s integrations -v
```

## 3. 初始化与凭据

### 3.1 创建 data-dir 和提示词目录

```bash
.venv-local/bin/prompt-studio init
```

默认目录：

```text
data/prompt-studio/
├── auth/                 # auth stub 的私有状态
├── prompts/              # agent_prompt.txt / snapshot_rules.txt / reply_markdown_rules.txt
├── gateway.sqlite3       # gateway 缓存
├── logs/                 # 常驻服务日志
└── runs/<run-id>/        # 每次 replay 的 advisor DB / 日志
```

`init` 从已安装的 `llm-gateway` 复制提示词；已有文件默认保留，`--force` 才覆盖。

### 3.2 提交 NSDP/Jira 凭据

advisor 建会话前会校验 NSDP/Jira 凭据。提交真实模型所需凭据：

```bash
.venv-local/bin/prompt-studio submit-tokens \
  --nsdp-token-file /path/nsdp.txt \
  --jira-token-file /path/jira.txt
```

token 文件建议 `chmod 600`。凭据会持久化在该 data-dir 的 `auth/` 下，后续 `replay` / `serve` 直接复用。token 不会回显、不会写入结果或日志。

## 4. 准备输入

### 4.1 admin-site TXT

从 admin-site 导出“会参谋 · 文字记录”TXT，直接作为 `--transcript` 或粘贴到 Web UI：

```text
会参谋 · 文字记录
会话 s-old · 会议 111111111 · 预算会
导出时间 2026/11/26 16:00:00 · 共 2 条

[2026/11/26 16:02:03] 本机: 我们过一下预算
[2026/11/26 16:02:10] 未识别: 我补充一点
```

规则：

- 只有人类发言会被重放；`LLM` 行仅作历史对照。
- `未识别` 记为无 speaker。
- 不以 `[` 开头的行会拼接到上一条记录。
- 时间只用于展示，回放使用当前时间。

### 4.2 场景 JSON

Web/CLI 可保存和复用场景：

```json
{
  "title": "预算会",
  "description": "本次会议讨论预算",
  "documents": [
    {"kind": "doc", "title": "预算表", "text": "总预算 500 万"}
  ],
  "turns": [
    {"kind": "human", "speaker": "本机", "text": "我们过一下预算"},
    {"kind": "human", "speaker": null, "text": "我补充一点"}
  ]
}
```

使用：

```bash
.venv-local/bin/prompt-studio replay --scene meeting.json
```

`description` 和 `documents` 会在建会话后、第一条字幕前注入 advisor 的 material；不需要改 advisor 代码。

## 5. 命令行重放

### 5.1 真实模型

```bash
.venv-local/bin/prompt-studio replay \
  --transcript meeting.txt \
  --description "预算评审" \
  --model nio/DeepSeek-V4.1-Flash \
  --opencode-auth ~/.local/share/opencode/auth.json \
  --opencode-provider-config /path/to/provider.json
```

- `opencode` 必须在 `PATH` 中。CLI 固定使用 `opencode`，没有 `--backend` 选项。
- `--opencode-auth` 指向包含对应 provider API key 的 `auth.json`。
- 自定义 provider 需要 `--opencode-provider-config`。
- 真实 NSDP/Jira token 必须已提交，否则 scoped tool 查询会失败。

### 5.2 auto / paced

```bash
# 一次发完所有人类字幕，等待最终决策
prompt-studio replay ... --mode auto

# 每行之间等待 5 秒，发送期间不等待模型回复
prompt-studio replay ... --mode paced --paced-seconds 5
```

`paced` 模式适合模拟真实会议：字幕按间隔持续到达，模型任务会和后续字幕重叠。`--paced-seconds 0` 表示尽快发送。`--json` 只输出结果 JSON。

### 5.3 其他参数

- `--title`：覆盖场景标题。
- `--document <file>`：追加文档（可重复），title 取文件名。
- `--meeting-no`：6–12 位数字；默认随机新会议号。
- `--run-id`：字母/数字/点/下划线/短横线，不能含 `..`。
- `--owner`：固定 owner，默认 `ou_prompt_studio`。
- `--mode`：`auto` 一次灌完；`paced` 按间隔发送。
- `--paced-seconds`：paced 模式每行间隔秒数，默认 5；0 表示尽快。
- `--data-dir` / `--prompt-dir`：覆盖默认目录。
- `--nsdp-token-file` / `--jira-token-file`：本次运行临时提交凭据。

## 6. Web UI

### 6.1 默认：只绑 loopback

```bash
.venv-local/bin/prompt-studio serve --port 8765
```

本机打开 `http://127.0.0.1:8765`。远端通过 SSH 隧道：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
# 然后在本地打开 http://127.0.0.1:8765
```

### 6.2 可信内网直连

```bash
.venv-local/bin/prompt-studio serve \
  --host 0.0.0.0 --port 8765
```

启动后会打印：

```text
prompt-studio: listening on 0.0.0.0:8765
prompt-studio: http://<server-ip>:8765
警告: UI 未认证，已绑定所有网卡；仅限可信内网，并配防火墙。
```

只允许 `--host 127.0.0.1`（默认）或 `--host 0.0.0.0`。UI 没有登录、没有权限模型：任何能访问端口的人都能启动 replay、修改提示词、切换模型、读取会议内容。**只允许可信内网，并配安全组/防火墙；绝不能暴露公网。**

### 6.3 页面操作

1. 粘贴 TXT，填写标题/会议描述。
2. 选择 `auto` 或 `paced`；`paced` 可设置每行间隔秒数。
3. 点“解析”检查识别结果，再点“开始重放”。
4. 右侧查看：
   - 每条人类字幕对应的 decision / reply
   - `jobs`、`complete`、`replies` 数量
   - 每条 job 的 `control`、`reply`、snapshot 摘要
   - material（description/documents/state/context_version）
   - advisor 日志路径
5. “提示词”区域可在线编辑三个 prompt 文件；保存为原子写。
6. 顶部可切换模型；切换会重启 gateway。

运行中禁止修改提示词或切换模型。提示词是完整链路：`agent_prompt.txt + snapshot_rules.txt + reply_markdown_rules.txt`，只改一个可能不够。

### 6.4 启动配置

`serve` 支持与 `replay` 相同的模型/token 参数；默认模型为 `nio/DeepSeek-V4.1-Flash`。paced 间隔在 Web UI 上设置：

```bash
.venv-local/bin/prompt-studio serve \
  --model nio/DeepSeek-V4.1-Flash \
  --opencode-auth ~/.local/share/opencode/auth.json \
  --opencode-provider-config /path/to/provider.json \
  --nsdp-token-file /path/nsdp.txt \
  --jira-token-file /path/jira.txt
```

未显式传 prompt-dir 时，`serve` 会确保 `data-dir/prompts` 存在；缺文件时从 gateway 内置 prompts 复制。

## 7. 飞书文档抓取（可选）

```bash
# my-lark 在 stable profile 默认关闭
noam features enable my-lark        # 或每条命令加 --noam-profile dev
noam my-lark auth qr-login

# 追加到场景 documents[]
.venv-local/bin/prompt-studio doc fetch \
  --url 'https://nio.feishu.cn/docx/...' \
  --scene meeting.json --noam-profile dev

# 或导出 markdown，再用 --document 重放
.venv-local/bin/prompt-studio doc fetch \
  --url 'https://nio.feishu.cn/wiki/...' \
  --output doc.md --noam-profile dev
```

限制与行为：

- 只接受 `https://*.feishu.cn` / `https://*.larksuite.com` 的 `/docx/`、`/wiki/` URL。
- URL 作为单个 argv 传给 `noam my-lark docs +fetch`，不经 shell。
- studio 不保存飞书 session cookie。
- 抓取失败返回非零并提示配置；可退回 `replay --document <file>` 手填。
- `--noam` 可指定可执行文件；`--account` 选择 my-lark 账号；`--timeout` 默认 90 秒。

## 8. 结果怎么读

CLI `--json` 与 Web 结果都来自 advisor DB 的只读查询：

- `records`：本次 replay 的人类字幕与 `channel='local'` 的 LLM reply，按时间排序。
- `jobs`：每次决策的 generation、state、role_version、decision、timings。
- `snapshot_summary`：
  - `captions`：进入 snapshot 的字幕数
  - `fresh_input_keys`：新鲜输入 key
  - `reply_allowed_keys`：允许回复的 key
  - `pending_reply`：是否有待回复
  - `meeting_context`：是否注入 material
  - `context_omitted`：上下文是否被省略
- `material`：实际注入的 description/documents/state/context_version。
- `advisor_log`：该 run 的 advisor 日志路径。

常见决策：

- `control=none` + `reply=...`：正常回答。
- `control=ignore`：认为不需要回复。
- 有待审批/待投递状态时，`pending_reply=true` 可能是设计行为，不一定是错误。
- 没有 `complete` job：看 `session_state.error` 与 advisor log。

## 9. 安全与隔离

- 每次 run 使用新的 meeting_no、新 session、fresh advisor data-dir。
- auth 使用 `--stub`，飞书不会收到真实订阅/审批请求。
- 真实 NSDP/Jira token 只存在 data-dir/auth；不写日志、不入库到结果。
- UI 默认 loopback；`0.0.0.0` 仅供可信内网，必须配防火墙。
- advisor ingest 端口由 advisor 绑 `0.0.0.0`；隔离机同样需要限制来源。
- 不要把 `data/prompt-studio/` 提交到 git。
- 每次 run 的 `runs/<run-id>/` 会保留 advisor DB 和日志；确认不需要后可删除。

## 10. 常见问题

| 现象 | 处理 |
|---|---|
| `authorization_required: join credentials missing` | 先 `submit-tokens`，或给命令传 `--nsdp-token-file`/`--jira-token-file`。 |
| `prompt dir missing files` | 运行 `init`，或给 `--prompt-dir` 指向包含三个 prompt 文件的目录。 |
| `run directory already exists` | `--run-id` 重复；换一个 run-id 或删除对应 `runs/<id>`。 |
| `healthz timeout` / advisor exited | 看结果里的 advisor log 或 `data/prompt-studio/logs/`；常见是端口/依赖/opencode 启动失败。 |
| `captions did not land` | fake STT/ingest 未连通或 advisor 未订阅；看 advisor log 与 STT 连接。 |
| `decision not settled` | 模型慢/超时/工具卡住；看 job timings、gateway 日志和 advisor log。 |
| 有字幕但没有 reply | 先看 `control` 与 `pending_reply`；`ignore`/待审批是正常决策。 |
| 改了提示词不生效 | 运行中禁止编辑；保存后开新 run 最稳。确认三个 prompt 文件是一套。 |
| 切换模型后失败 | 确认 `opencode`、auth.json、provider config 和 model id；gateway 会重启。 |
| token 过期 | NSDP/Jira token 无 refresh，重新 `submit-tokens`。 |
| `my-lark is disabled` | `noam features enable my-lark`，或加 `--noam-profile dev`。 |
| 文档抓取返回不是 JSON/无文本 | 先 `noam my-lark auth qr-login`；空文档或不支持的返回格式会失败，可手填 `--document`。 |
| 内网打不开 UI | 确认 `--host 0.0.0.0`、端口、安全组；不要用 `0.0.0.0` 当浏览器地址，用服务器内网 IP。 |

## 11. 已知边界

- CLI 固定使用真实 `opencode`；fake backend 仅用于仓库自动化测试，不评估提示词质量。
- 每次 run 仍是人工指定的场景；不是通用压测/评测平台。
- 不重放历史 LLM 回复到 `sent_replies`。
- 不做 bot/IM 分发、订阅审批、GUI。
- 不做生产部署、反代、HTTPS 证书；公网访问请用 SSH 隧道或带认证的网关。
- 模型只支持“重启 gateway 切换”，不支持 per-request 热切换。
- NSDP catalog/外部工具查询可能让首个决策变慢；这是 advisor/gateway 的真实行为。
