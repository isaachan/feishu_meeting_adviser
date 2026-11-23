# 生产 · 更换 LLM 模型（runbook，2026-11-23）

状态：运维 runbook，随 `llm-gateway` ≥ 0.1.1 生效。适用于**决策链路**（advisor → llm-gateway → OpenCode 后端，`REMOTE_BACKEND=opencode`）；不含 TUI/本地交互式 opencode。更换频率低（月级），因此不做热切换：改配置 + 重启 `llm-gateway` 即可。
上游：[DEPLOYMENT](DEPLOYMENT.md) §7、[ARCHITECTURE](ARCHITECTURE.md)、[LLM_GATEWAY_PERFORMANCE_MEASUREMENT](LLM_GATEWAY_PERFORMANCE_MEASUREMENT.md)。

## 1. 为什么不能只改 opencode 的配置文件

gateway 每次决策都起一个隔离的 opencode 实例（`llm-gateway/src/llm_gateway/backends/opencode.py`）：

- `XDG_{CONFIG,DATA,CACHE,STATE}_HOME` 指向 `run-*` 私有目录，**`~/.config/opencode/opencode.jsonc` 不参与**；
- 整份 `OPENCODE_CONFIG_CONTENT` 由代码生成（模型、`enabled_providers`、MCP、权限、agent prompt）；
- 继承环境里所有 `OPENCODE_*` 变量会被删除，无法从外部注入配置；
- opencode 目录（models.dev）之外的 provider 必须由代码写进 `config['provider']`，为此新增了 `--opencode-provider-config`。

所以「某个 provider 在 TUI 的 `/models` 里可见」与「gateway 能用它」是两件事。TUI 配置可保留，与本文互不影响。

## 2. 配置项（全部写在 `scripts/prod.env`，shell 格式、需 `export`）

| 变量 | 作用 | 缺省 |
| --- | --- | --- |
| `OPENCODE_BIN` | opencode 可执行文件（必填） | — |
| `OPENCODE_MODEL` | `<provider>/<model>`，必须恰好一个 `/` | `zai-coding-plan/glm-5.3` |
| `OPENCODE_AUTH` | auth.json 路径 | `~/.local/share/opencode/auth.json` |
| `OPENCODE_PROVIDER_CONFIG` | 自定义 provider 定义 JSON 路径（目录外 provider 必填） | 无 |
| `<PROVIDER>_API_KEY` | provider 用 `options.apiKey='{env:VAR}'` 引用时，需在此导出同名变量 | 无 |

`prod-run.sh` 会 `source` 该文件并 `export`，变量因此也能被 opencode 子进程继承；systemd 单元本身只设 `HOME`。auth.json 即使不使用（改用 env 提供 key）也**必须存在且可读**，否则每次决策 `backend_unavailable`。

## 3. 切换步骤

### 3.1 同一 provider 内换 model（最常见）

```bash
# prod 上，scripts/prod.env
export OPENCODE_MODEL='zai-coding-plan/glm-5.3-flash'
```

无活跃会话时重启：`sudo systemctl restart meeting-advisor@llm-gateway`。**不需要** `prod-up.sh`、不需要重装 venv。

### 3.2 换到 opencode 内置目录中的 provider

1. 在 auth.json（或 `OPENCODE_AUTH` 指向的文件）里加该 provider 条目：
```json
{ "<provider>": { "type": "api", "key": "<key>" } }
```
2. `OPENCODE_MODEL='<provider>/<model>'`，重启 gateway。

auth.json 要求：非符号链接、≤1 MB、条目为 `type=api` + 非空 `key`。

### 3.3 换到目录外 provider（如 `nio` / NIO ModelSight）

1. 准备 provider 定义 JSON（建议放部署用户家目录，不要提交 Git，`chmod 600`）：
```json
{
  "nio": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "NIO ModelSight",
    "api": "https://model.nioint.com/v1",
    "options": { "apiKey": "{env:MODELSIGHT_API_KEY}" },
    "models": { "DeepSeek-V4.1-Flash": { "name": "DeepSeek V4.1 Flash" } }
  }
}
```
2. `scripts/prod.env`：
```bash
export OPENCODE_MODEL='nio/DeepSeek-V4.1-Flash'
export OPENCODE_PROVIDER_CONFIG='/home/<user>/opencode-provider.json'
export MODELSIGHT_API_KEY='<key>'
```
3. 重启 gateway。

代码对该文件的约束：非符号链接、≤100 KB、顶层为 `{provider: object}`，且**必须包含 `OPENCODE_MODEL` 里选中的 provider**，否则启动后每次决策都 `backend_unavailable`（`verify_provider` 阶段失败）。key 建议走 `{env:VAR}`，避免明文进 auth.json；两种方式（auth.json 条目或 env key）择一即可。

### 3.4 涉及代码变更时

本 runbook 依赖 `llm-gateway` ≥ 0.1.1（`--opencode-model` 从 `prod.env` 透传、`--opencode-provider-config`）。若 prod 仍是旧版本，需按 [DEPLOYMENT](DEPLOYMENT.md) §7.2 正常发布：

```bash
scripts/prod-down.sh            # 有 recording/draining 会话时先 --drain
git pull                        # 工作区须干净
scripts/prod-up.sh              # 重装 venv、重写 systemd/nginx、自检
```

之后模型/密钥的变更都只是 3.1–3.3 的配置改动，不再需要发布。

## 4. 验证

配置生效只是必要条件，`/healthz` 不校验模型，必须跑一次真实决策：

```bash
# 1) 进程参数里有新模型
ps -eo args | grep '[l]lm-gateway serve'
.venv-prod/llm-gateway/bin/llm-gateway --version     # 期望 ≥ 0.1.1

# 2) 直接打一次决策（不需要开会，不需要 advisor/recorder）
cd harness && .venv/bin/llm-harness --owner ou_<open_id> --url http://127.0.0.1:8377
me: 我们 NT3 的 SW Gate 是 11 月 20 号吗？
/decide

# 3) 日志：一次成功决策应出现完整阶段链，且无失败标记
tail -f data/prod/logs/gateway.log
grep -c backend_unavailable data/prod/logs/gateway.log
```

判定依据：gateway 会校验 opencode 回传的 `providerID`/`modelID` 是否等于配置值（不等即 `output_invalid`），并在 `verify_provider` 阶段确认 provider 已连接且模型在目录中。**一次成功的 decision 本身就证明是新模型产出的**。想再加一层外部证据：决策期间 `ss -tnp | grep 443` 应连到 provider 域名，或核对 ModelSight 控制台调用量。

注意 `/decide` 之外的普通文本只是累积，不会触发推理；首次跑 harness 需 `python3 -m venv .venv && .venv/bin/pip install -e .`。工具调用是否正常看日志里的 `tool completed`。

## 5. 回滚

去掉 `OPENCODE_MODEL`（回到默认 `zai-coding-plan/glm-5.3`）或改回旧值，然后：

```bash
sudo systemctl restart meeting-advisor@llm-gateway
```

`OPENCODE_PROVIDER_CONFIG` 与新增的 API key 可以留着，不影响默认模型。回滚同样**不是热切换**：重启瞬间在跑的决策会失败，建议先确认无活跃/排空中的会议（admin-site 会话列表或 `advisor` API）。

## 6. 常见失败与定位

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 每次决策 `backend_unavailable`，日志 `failed_stage=verify_provider` | provider JSON 未加载、模型名写错、provider 未 connected（key 缺失/无效） | 检查 `ps` 参数、JSON 中 model 键、API key 是否导出 |
| `process_start` 阶段即 `backend_unavailable` | auth.json 缺失/不可读/格式错（即便用 env key 也会读） | 修复或指定 `OPENCODE_AUTH` |
| `output_invalid` 持续出现 | 模型不按 schema 输出 JSON、误用工具、或调用了未授权工具 | 换模型；对比 `opencode.stderr` 尾部 |
| `execution_timeout` | 模型过慢，超出 advisor 的 `model_timeout_seconds`（220s） | 换更快的 model/档位 |
| TUI 里能选但 gateway 报 provider 不存在 | 只改了 `~/.config/opencode` | 按 §3.3 提供 provider JSON |

## 7. 安全与文件位置

- `scripts/prod.env`、provider JSON、auth.json 均不入 Git（前者本就被忽略/部署时 `chmod 600`）；provider JSON 建议置于仓库外，避免误提交。
- 不要把 API key 写进 provider JSON 的 `options.apiKey` 字面量；用 `{env:VAR}` + `prod.env`。
- 一个 provider 定义文件可同时保留多个 provider，切换时只改 `OPENCODE_MODEL`；但其中必须包含当前选中的 provider。
