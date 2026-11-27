# prompt-studio

本地提示词测试平台：把 admin-site 导出的会议文字记录（TXT）重新灌进真实的 advisor + llm-gateway，观察会参谋的 decision/reply。

设计与任务拆分见 [docs/PROMPT_STUDIO.md](../docs/PROMPT_STUDIO.md)。本模块不导入其他模块源码，只通过 HTTP/WS/子进程与 `auth`、`advisor`、`llm-gateway` 交接。

## 安装

在仓库根 venv 中安装（`auth`/`advisor`/`llm-gateway` 也需已安装）：

```bash
.venv-local/bin/pip install -e ./prompt-studio
.venv-local/bin/prompt-studio --help
```

## 快速开始

```bash
# 1) 创建 data/prompt-studio 与 prompts/（从 llm-gateway 复制）
.venv-local/bin/prompt-studio init

# 2) 提交一次 NSDP/Jira token（值不会回显；也可用 --*-token-file）
NSDP_TOKEN=... JIRA_TOKEN=... .venv-local/bin/prompt-studio submit-tokens

# 3a) Web UI（只绑 loopback；远端请用 SSH 隧道）
.venv-local/bin/prompt-studio serve --backend opencode

# 3b) 或命令行重放
.venv-local/bin/prompt-studio replay --transcript meeting.txt \
  --description "预算评审" --mode auto --model zai-coding-plan/glm-5.3

# 无模型依赖的确定性验证
.venv-local/bin/prompt-studio replay --transcript meeting.txt --backend fake
```

`--mode step` 在每条人类字幕落库并产生决策后停顿，交互终端按 Enter 继续；`--json` 只输出结果 JSON。默认 data-dir 为 `data/prompt-studio/`，prompt 目录为其中的 `prompts/`。

advisor 建会话始终需要 NSDP/Jira 凭据（即使 `--backend fake`）。第 2 步会把凭据持久化到 data-dir，之后的 `replay`/`serve` 复用；也可以直接给 `serve`/`replay` 传 `--nsdp-token-file`/`--jira-token-file`。

## Web UI

- 粘贴 TXT，填写标题/会议描述，选择 `auto` 或 `step` 后开始。
- `step` 模式用“下一句”推进，“结束”收尾；`auto` 一次跑完。
- 右侧展示每条决策、reply、snapshot 摘要、material 与完整记录。
- 提示词在线编辑；保存为原子写。运行中禁止修改提示词或切换模型。
- 切换模型会重启 gateway；NSDP catalog 按 session 冻结，首个决策可能较慢。

## 隔离 server

UI 只监听 `127.0.0.1`，不要在公网直接暴露。远端部署：

```bash
ssh -L 8765:127.0.0.1:8765 user@server
# 然后在本地打开 http://127.0.0.1:8765
```

token 只从环境变量或 `--nsdp-token-file`/`--jira-token-file` 读取，不会写入日志或结果。advisor 的 ingest 端口当前绑 `0.0.0.0`，隔离 server 需配合防火墙/安全组限制来源。

## 进程模型

`serve` 启动 auth(stub)、llm-gateway 与 fake STT；每次 run 另起一个 fresh advisor，避免旧 session 的未投递消息与 single-claim 干扰。Ctrl-C 会停止所有子进程。

## 常见问题

- **`authorization_required: join credentials missing`**：先跑 `submit-tokens`（或给命令传 token 文件）。`--backend fake` 仍需要凭据，因为 advisor 建会话前会校验 NSDP/Jira。
- **第一个决策很慢**：每个 session 会后台执行 `noam nsdp info` 拉取版本目录；等待即可。没有 catalog 时早期决策可能缺少版本事实。
- **有字幕但没有 reply**：看右侧 `decision.control` 与 `snapshot_summary.pending_reply`。`ignore`/`pending` 是正常决策；错误看结果里的 advisor log 路径。
- **改了提示词不生效**：gateway 在每次 decide 重新读取文件；运行中平台禁止编辑。最稳妥是保存后开新 run，并确认三个提示词文件是一套完整链路。
- **端口被占用/远端访问**：用 `--port` 换端口；远端只做 `ssh -L 8765:127.0.0.1:8765`，不要把 UI 反代到公网。advisor ingest 端口由 advisor 绑 `0.0.0.0`，隔离机需用防火墙限制来源。
- **token 过期**：NSDP/Jira token 无 refresh，重新 `submit-tokens` 即可。
- **磁盘增长**：每次 run 在 `data/prompt-studio/runs/<run-id>/` 保留独立 advisor DB 与日志；确认不再需要后可整目录删除。
