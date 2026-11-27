# prompt-studio

本地提示词测试平台：把 admin-site 导出的会议文字记录（TXT）重新灌进真实的 advisor + llm-gateway，观察会参谋的 decision/reply。

设计与任务拆分见 [docs/PROMPT_STUDIO.md](../docs/PROMPT_STUDIO.md)，完整操作见 [使用手册](../docs/PROMPT_STUDIO_MANUAL.md)。本模块不导入其他模块源码，只通过 HTTP/WS/子进程与 `auth`、`advisor`、`llm-gateway` 交接。

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

# 3a) Web UI（默认只绑 loopback；远端用 SSH 隧道）
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

## 飞书文档抓取（可选）

用个人飞书 web session 抓取 docx/wiki 正文，避免手工复制：

```bash
# 一次性：my-lark 在 stable profile 默认关闭，dev profile 可用
noam features enable my-lark        # 或每条命令加 --noam-profile dev
noam my-lark auth qr-login

# 追加到场景 documents[]（推荐）
.venv-local/bin/prompt-studio doc fetch --url 'https://nio.feishu.cn/docx/...' \
  --scene meeting.json --noam-profile dev

# 或只导出 markdown，再用 --document 重放
.venv-local/bin/prompt-studio doc fetch --url 'https://nio.feishu.cn/wiki/...' \
  --output doc.md --noam-profile dev
```

只接受 `https://*.feishu.cn` / `https://*.larksuite.com` 的 `/docx/`、`/wiki/` URL；URL 作为单个 argv 传给 noam，不经 shell。studio 不保存 session cookie。抓取失败时命令返回非零并提示配置；可退回 `replay --document <file>` 手填。

## 隔离 server

UI 默认只监听 `127.0.0.1`。远端访问：

```bash
# 方式 A：SSH 隧道（默认，推荐）
ssh -L 8765:127.0.0.1:8765 user@server
# 然后在本地打开 http://127.0.0.1:8765

# 方式 B：可信内网直连（UI 没有登录）
.venv-local/bin/prompt-studio serve --host 0.0.0.0 --port 8765
# 在服务器内网 IP 上打开 http://<server-ip>:8765
```

`--host 0.0.0.0` 会绑定所有网卡并在启动时告警。任何能访问端口的人都能启动 replay、修改提示词、切换模型、读取会议内容；只允许可信内网，并用安全组/防火墙限制来源，绝不能暴露公网。

token 只从环境变量或 `--nsdp-token-file`/`--jira-token-file` 读取，不会写入日志或结果。advisor 的 ingest 端口当前绑 `0.0.0.0`，隔离 server 需配合防火墙/安全组限制来源。

## 进程模型

`serve` 启动 auth(stub)、llm-gateway 与 fake STT；每次 run 另起一个 fresh advisor，避免旧 session 的未投递消息与 single-claim 干扰。Ctrl-C 会停止所有子进程。

## 常见问题

- **`authorization_required: join credentials missing`**：先跑 `submit-tokens`（或给命令传 token 文件）。`--backend fake` 仍需要凭据，因为 advisor 建会话前会校验 NSDP/Jira。
- **第一个决策很慢**：每个 session 会后台执行 `noam nsdp info` 拉取版本目录；等待即可。没有 catalog 时早期决策可能缺少版本事实。
- **有字幕但没有 reply**：看右侧 `decision.control` 与 `snapshot_summary.pending_reply`。`ignore`/`pending` 是正常决策；错误看结果里的 advisor log 路径。
- **改了提示词不生效**：gateway 在每次 decide 重新读取文件；运行中平台禁止编辑。最稳妥是保存后开新 run，并确认三个提示词文件是一套完整链路。
- **端口被占用/远端访问**：用 `--port` 换端口；默认走 `ssh -L 8765:127.0.0.1:8765`。可信内网可 `--host 0.0.0.0` 直连，但 UI 无认证，必须配防火墙；不要把 UI 暴露到公网。advisor ingest 端口由 advisor 绑 `0.0.0.0`，隔离机同样需限制来源。
- **token 过期**：NSDP/Jira token 无 refresh，重新 `submit-tokens` 即可。
- **磁盘增长**：每次 run 在 `data/prompt-studio/runs/<run-id>/` 保留独立 advisor DB 与日志；确认不再需要后可整目录删除。
