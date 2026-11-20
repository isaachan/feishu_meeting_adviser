# harness (llm-harness)

交互式测试 REPL：模拟 advisor 的模型客户端（`CONTRACT_REMOTE_INFERENCE` 的 submit/poll 语义），手动输入会议文字，经 [llm-gateway](../llm-gateway) 观察 LLM 的 decision-v1 输出。不跑全 stack（recorder/advisor/bot/auth），只测"会议文字 → LLM 判断"这一段。

## 机制对照（advisor → harness）

advisor 不是逐句触发 LLM：supervisor 每 0.5s 轮询，仅当"无运行中任务、过 backoff、存在 `generation > decided_generation` 的新句"时发起一次 decide，批 = 自上次判断以来的所有新句（freshness 窗口 240s）。harness 用手动 `/decide` 复现该批语义：输入只累积，`/decide` 时 fresh_keys = 所有未判新句。

## 运行

```bash
cd harness
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/llm-harness --owner ou_xxxxxxxx --url http://127.0.0.1:8377 [--backend opencode|fake] [--deadline 90]
```

`--owner` 必填（`ou_` 开头的 open_id）：网关按 `CONTRACT_INFERENCE_IDENTITY` v2 要求组织者身份，NSDP/Jira 工具以该身份授权。

## REPL

- 普通文本行 = 一条 caption，只累积不触发。可选说话人前缀：`me:`/`我:`/`本机:` → `speaker_name=本机`；`其他:`/`远程:` → 匿名远端（前缀剥掉）；无前缀 = 匿名远端（与现状一致：sys 音频无说话人分离）
- `/decide` 提交一次判断并阻塞等待。无新句时真实 advisor 不会发起（`prepare_job` 空转），harness 会提示后仍强制提交，request_id 加 `-rN` 后缀绕过网关幂等
- `/show` 打印当前 snapshot JSON（含 generation/未判数）
- `/reset` 清空会议；`/help`；`/quit` 退出

结果展示：decision JSON、按 `Decision.parse` 同款规则的合法性校验、耗时与网关 timings、request_id。

## 与真实 advisor 的简化

- caption 时间戳用输入时刻生成（freshness/reply-evidence 窗口 240s/600s 与真实默认一致）
- decision 的 reply 直接记入 `sent_replies`（真实链路经 pending_reply → bot 发送）
- 组织者身份经 `--owner` 传入并绑定推理任务（NSDP/Jira 工具以该身份执行），但不做会话预热（`context-sessions`）与 meeting_context 材料、上下文预算的 result 级裁剪（仅逐句跳过 + `context_omitted` 标记）

## 开发与测试

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```
