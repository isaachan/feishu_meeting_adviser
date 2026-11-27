# Prompt Studio（会参谋提示词测试平台）：设计与任务拆分

状态：2026-11-27 设计定稿，按本文任务依次实施。本文只描述本地/隔离环境下的提示词测试工具，不改变生产链路、部署形态或现有契约。

## 1. 目标

在同一台隔离服务器上，把 admin-site 导出的会议文字记录（TXT）重新灌进**真实的 advisor + 真实的 llm-gateway**，观察会参谋的 decision/reply，用于快速迭代提示词与模型。

- 输入：agent prompt 目录、会议描述/文档、admin TXT 字幕、模型、replay 模式。
- 输出：每个决策点的 snapshot、decision JSON、reply、evidence key 合法性、timings/工具调用；历史 LLM 行只作对照。
- 隔离：独立 data-dir、独立端口、`auth --stub`（飞书）+ 真实 NSDP/Jira token；不接触 dev/prod。

非目标：bot/IM 分发、订阅审批、GUI、生产部署、热切换模型、per-request 模型、上下文重放（把历史 LLM 回复灌回）。

## 2. 关键决策（已对齐）

1. **不重写 snapshot**：新增一个 dev-only 的脚本化 STT（实现 `CONTRACT_STT_STREAM`），把字幕作为 `final` 灌进真实 advisor；advisor 的批处理/窗口/evidence/材料全部复用。
2. **产品代码只改 gateway 一处**：新增 `--prompt-dir`，把 prompt 从 import 时加载改为**每次 decide 读取**（默认仍是打包 prompts，行为不变）。模型切换仍靠重启 gateway（`--opencode-model`）。
3. **会议描述/文档按会话注入**：创建 session 后、第一条字幕前，直接 patch advisor 的 `session_materials` 与 `sessions.material_version/material_state`；不改 advisor 代码，也不把 description 做成启动参数。
4. **会话隔离**：固定真实 owner（真实 NSDP/Jira token 只提交一次）+ **每次 run 新 9 位 meeting_no + 新 session** + **每次 run 一个 fresh advisor data-dir**，避开 single-claim 与未投递 IM 行导致的 drain 卡住。
5. **clean replay**：只重放人类字幕（TXT 中非 `LLM` 行）；历史 `LLM` 行仅作对照展示，不进入 `sent_replies`。
6. **观察不依赖 bot**：decision 落库时 advisor 会写 `channel='local'` 的 outbox；读 `GET /admin/sessions/{id}/transcript` 即可看到 LLM 行，control/evidence/timings 读 advisor `jobs` 表。
7. **隔离 server**：UI 只绑 `127.0.0.1`，通过 SSH 隧道访问；token 只从环境变量/私有文件读取，不写日志、不入库。

## 3. 架构与数据流

```text
Prompt Studio (本地/隔离机, 127.0.0.1)
  ├─ Web/CLI
  ├─ auth --stub            (飞书 stub；真实 NSDP/Jira token 按固定 owner 提交)
  ├─ llm-gateway (opencode) (--prompt-dir，每次 decide 读 prompt；模型切换时重启)
  ├─ advisor (stub calendar)(每次 run fresh data-dir)
  ├─ scripted fake STT      (CONTRACT_STT_STREAM v2；按 TXT 发 final)
  └─ ingest keepalive       (CONTRACT_RECORDER_INPUT；发 start + 静音帧)

run:
  auth authorize(owner) ─┬─ 首次
  submit nsdp/jira token ┘
  POST /sessions(owner, meeting_no) → session_id/token/event
  patch session material (description/docs)
  ingest start → 首个音频帧 → advisor 连接 fake STT
  fake STT 按脚本发 final → advisor supervisor → gateway → decision
  读 transcript + jobs → 展示/对照
```

## 4. 模块与文件布局

新增独立模块 `prompt-studio/`（包名 `prompt_studio`，不导入其他模块源码；只经 HTTP/WS/子进程与 advisor/auth/gateway 交接）：

```text
prompt-studio/
├── pyproject.toml
├── README.md
├── src/prompt_studio/
│   ├── __init__.py
│   ├── __main__.py        # CLI: init / replay / serve
│   ├── transcript.py      # admin TXT 解析、可编辑模型、JSON 场景
│   ├── material.py        # advisor sqlite material patch
│   ├── clients.py         # auth / advisor HTTP 客户端
│   ├── stack.py           # 长驻子进程：auth / gateway / fake STT；healthz
│   ├── fake_stt.py        # 脚本化 STT WS 服务
│   ├── ingest.py          # ingest WS 客户端（start/frame/stream_end）
│   ├── replay.py          # 编排：建会话/patch/喂字幕/等决策/读结果
│   ├── results.py         # decision/log 读取与对照
│   ├── web.py             # 单次 run 的后台 worker 与 step 视图
│   ├── web_app.py         # UI 应用状态：parse/prompt/model/run
│   ├── web_http.py        # stdlib HTTP 路由与服务启动
│   └── static/index.html
└── tests/
```

依赖：Python ≥3.11，`websockets>=17,<18`；HTTP/SQLite/stdlib。dev：`coverage`、`pyright`、`ruff`。

数据目录：`data/prompt-studio/`（已在 `.gitignore` 的 `/data/` 下）
- `auth/`：stub 飞书 token + 真实 NSDP/Jira token（私有）
- `gateway.sqlite3` / `gateway.sqlite3.catalog.sqlite3`：跨 run 保留 release 缓存
- `runs/<run_id>/advisor/`：每次 run 一个 fresh advisor DB
- `prompts/`：固定 prompt 目录（gateway `--prompt-dir`）

## 5. 输入格式：admin TXT

admin-site 导出的 TXT（`admin-site/src/admin_site/static/index.html`）：

```text
会参谋 · 文字记录
会话 s-... · 会议 123456789 · 周会
导出时间 2026/11/26 16:00:00 · 共 42 条

[2026/11/26 16:02:03] 本机: 我们过一下预算
[时间未知] LLM: 预算结论是……
[2026/11/26 16:02:10] 未识别: 我补充一点
```

解析规则：
- 头部到第一个空行；可选解析 session/meeting_no/title。
- 记录行：`[<time>] <speaker>: <text>`；发言者可能含冒号，按 `] ` 后第一个 `: ` 切分，文档标注该边界。
- 不以 `[` 开头的行视为上一条记录的续行（`\n` 拼接）。
- `speaker == 'LLM'` → `kind='llm'`（仅对照）；`本机` 保留；`未识别` → speaker=None；其他原样保留。
- 时间：支持 `YYYY/M/D H:mm:ss` 与 `YYYY-MM-DD HH:mm:ss`；`时间未知`/无法解析 → None。
- 空文本不合法；一个场景至少一条人类字幕。

场景 JSON：`{"title","meeting_no","description","documents":[...],"turns":[{"kind","speaker","text","time_text"}]}`，用于 UI/CLI 保存与复用。

## 6. 运行模型与端口

- `Stack`：进程生命周期内启动并保持 `auth --stub` + `llm-gateway` + fake STT；端口全部用空闲端口。
- `ReplayRun`：每次 run 启动一个 fresh `advisor`（fresh `runs/<id>/advisor`），结束后停止；auth/gateway 不重启（模型切换显式重启 gateway）。
- 端口：auth / gateway / advisor api / advisor ingest / fake STT 均为临时端口；不占用 `scripts/local-up.sh` 的 8380/8381/8377/8400。
- timeout：advisor 决策用 `--model-timeout-s`（建议 220）；studio 等待加上余量；gateway deadline 上限 240。
- prompt：`data/prompt-studio/prompts/` 固定目录；UI 用临时文件 + `os.replace` 原子写；gateway 每次 decide 读取。
- 模型：`Stack.restart_gateway(model=...)`；同 provider 换 model 只改启动参数。

## 7. 结果与展示

每个决策点展示：
- snapshot 摘要：captions 数、fresh keys、reply_allowed keys、meeting_context 状态。
- decision JSON：`control`、`control_sentence_keys`、`reply`、`reply_sentence_keys`。
- 校验：evidence key 是否在 snapshot/context 中、reply 是否符合 decision-v2 Markdown 约束（由 gateway 校验）。
- timings：`execution_ms`、`tool_calls[]`、失败 stage/error。
- 对照：按顺序把本次 decision 与历史 `LLM` 行并排展示（不要求一一对应）。

## 8. 安全与隔离

- 只绑 `127.0.0.1`；不暴露公网；通过 SSH 隧道访问。
- NSDP/Jira token 从 `NSDP_TOKEN`/`JIRA_TOKEN` 或私有文件读取；不写入日志、不入库、不提交 Git。
- 真实 token 是长寿命凭据；过期需重新提交（studio 提供显式 `submit-tokens`）。
- data-dir 独立；不读写 dev/prod 的 `data/local`、`data/prod`。
- prompt 目录可以放仓库内供版本化，也可以放 data-dir；默认 data-dir，避免污染工作区。

## 9. 测试策略

- 纯单元：TXT 解析、场景 JSON、material patch、HTTP 客户端错误映射、脚本化 STT 协议。
- 模块内集成（fake backend）：起 `auth --stub` + `gateway(fake)` + `advisor(fake remote)` + fake STT，验证一次 replay 能落字幕、产生 decision、读回 reply。
- 可选真实模型（`LIVE_OPENCODE_TEST=1`）：用真实 gateway/opencode 跑一条短字幕；默认跳过。
- 所有任务 commit 前运行：`ruff check src tests`、`pyright`、`python -m unittest discover -s tests -v`；涉及跨模块时再跑 `integrations`。

## 10. 任务拆分

- [x] **PS1 · gateway `--prompt-dir` + 每次 decide 读取**
  - `prompts/__init__.py` 提供 `load_prompts(dir)`；保留默认常量兼容。
  - `OpenCodeBackend` 每次 `_environment` 读取 agent prompt；`ExecutionScheduler` 每任务读取 reply markdown rules。
  - `llm-gateway serve --prompt-dir`；默认 None = 打包 prompts。
  - 验收：默认输出不变；自定义目录生效；缺文件明确失败；gateway 单测全绿。

- [x] **PS2 · 模块骨架 + TXT 解析 + material patch + HTTP 客户端**
  - 新建 `prompt-studio/`，pyproject/README/包骨架。
  - `transcript.py`：解析/格式化/场景 JSON。
  - `material.py`：一事务 patch `session_materials` + `sessions.material_version/material_state`。
  - `clients.py`：auth authorize + token submit；advisor create/status/transcript/timings/end。
  - 验收：parser 覆盖头部/LLM/未知时间/续行/speaker 冒号；material patch 对最小 schema 生效；客户端错误映射。

- [x] **PS3 · 脚本化 fake STT + ingest keepalive**
  - `fake_stt.py`：v2 协议；`started`/`final`/`stopped`/`error`；按 `source_id` 路由；可单步/自动发字幕。
  - `ingest.py`：`start` + 静音帧 + `stream_end`。
  - 验收：协议单测；advisor 能连上并落字幕。

- [x] **PS4 · Stack 启动 + replay 编排 + 结果读取**
  - `stack.py`：auth/gateway/advisor 子进程、healthz、日志、停止。
  - `replay.py`：固定 owner/新 meeting_no/新 advisor；建会话 → patch → 喂字幕 → 等决策；step/auto。
  - `results.py`：读 `jobs`/transcript，输出决策记录。
  - 验收：fake backend 下端到端产出至少一个 decision + reply；step 模式在每条后等待。

- [x] **PS5 · CLI + 配置**
  - `prompt-studio init`：创建 data-dir/prompts（从已安装 gateway prompts 复制）。
  - `prompt-studio replay --transcript ... --description ... --mode step|auto --model ...`
  - `prompt-studio submit-tokens`：从 `NSDP_TOKEN`/`JIRA_TOKEN` 或 token 文件提交，不回显 token。
  - `serve` 在 PS6 接入。
  - 验收：命令可跑通 fake backend；参数错误清晰；不泄露 token。

- [x] **PS6 · 极简 Web UI**
  - `web.py`/`web_app.py`/`web_http.py` + `static/index.html`：导入/编辑 TXT、设描述、prompt 编辑、模型切换、step/auto、结果与 snapshot/decision 展示。
  - `prompt-studio serve --host 127.0.0.1 --port 8765`：只绑 loopback；prompt 原子写、运行中禁止改 prompt/模型。
  - 验收：HTTP 路由单测覆盖 auto/step fake replay、prompt 读写/越界、模型切换；`tests/test_web.py` 全绿。

- [x] **PS7 · 隔离/文档/CI 接线**
  - `prompt-studio/README.md`：隔离 server 部署、token、SSH 隧道、常见问题；说明 advisor ingest 仍绑 `0.0.0.0` 的防火墙边界。
  - `.github/workflows/quality.yml` 加 `prompt-studio`（module + paths），integration 装 `./prompt-studio[dev]` 并跑其全部测试（fake backend，无需真实模型）。
  - 根 README 模块列表补充。
  - 验收：CI 路径触发正确；本地 `ruff/pyright/unittest` 与 `integrations` 全绿。

- [ ] **PS8 ·（可选）飞书文档抓取**
  - `prompt-studio doc fetch --url <feishu-doc>` 走个人 `noam`/my-lark，把正文写入场景 `documents[]`。
  - 验收：能抓一篇文档并注入 material；失败时降级为手填。默认不阻塞 PS1–PS7。

## 11. 风险与已知边界

- **NSDP catalog 按 session 冻结**：每个新 session 会后台跑一次 `noam nsdp info`（30–60s）；最早几条决策可能没有 catalog。与生产行为一致；后续可用缓存预置 catalog 行优化。
- **speaker**：首版只开 loopback，所有字幕匿名（advisor 仅对 mic 源写 `speaker_name=本机`）。需要“本机”区分时再加 mic 通道。
- **真实 token 过期**：NSDP/Jira token 无 refresh；过期需显式重新提交。
- **drain/IM**：fresh advisor 每 run 丢弃旧 DB，不触发未投递 IM 行的 drain 等待；若改为长跑单一 advisor，需要 `--drain-deadline-s` 兜底。
- **fake backend 不能评估提示词质量**：fake 只验证链路；真实质量必须用 opencode + 真实模型。
- **prompt 目录是完整链**：`agent_prompt.txt + snapshot_rules.txt + reply_markdown_rules.txt`；只改 agent_prompt 可能不够。
- **并发/原子性**：prompt 文件必须原子写，避免 decide 读到半截；UI 默认在两次 run 之间切换 prompt。

## 12. 非目标

- 不做 bot/IM 真实分发、订阅审批、GUI 客户端。
- 不做生产部署/反代/HTTPS 证书。
- 不做模型热切换或 per-request 模型。
- 不做上下文重放（历史 LLM 回复灌回 `sent_replies`）。
- 不把 studio 变成通用压测/评测平台；每次 run 仍是人工指定的场景。
