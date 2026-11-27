# prompt-studio

本地提示词测试平台：把 admin-site 导出的会议文字记录（TXT）重新灌进真实的 advisor + llm-gateway，观察会参谋的 decision/reply。

设计与任务拆分见 [docs/PROMPT_STUDIO.md](../docs/PROMPT_STUDIO.md)。本模块不导入其他模块源码，只通过 HTTP/WS/子进程与 `auth`、`advisor`、`llm-gateway` 交接。

```bash
# 在仓库根 venv 中安装（需要同时安装 auth/advisor/llm-gateway）
.venv-local/bin/pip install -e ./prompt-studio

.venv-local/bin/prompt-studio --help
```

完整使用方式（隔离 server、token、SSH 隧道、step/auto replay）随实现补齐。

## 快速开始

```bash
# 在仓库根 venv 中安装（auth/advisor/llm-gateway 也需已安装）
.venv-local/bin/pip install -e ./prompt-studio

# 1) 创建 data/prompt-studio 与 prompts/（从 llm-gateway 复制）
.venv-local/bin/prompt-studio init

# 2) 提交一次 NSDP/Jira token（值不会回显；也可用 --*-token-file）
NSDP_TOKEN=... JIRA_TOKEN=... .venv-local/bin/prompt-studio submit-tokens

# 3) 用真实模型重放
.venv-local/bin/prompt-studio replay --transcript meeting.txt \
  --description "预算评审" --mode auto --model zai-coding-plan/glm-5.3

# 无模型依赖的确定性验证
.venv-local/bin/prompt-studio replay --transcript meeting.txt --backend fake
```

`--mode step` 在每条人类字幕落库并产生决策后停顿，交互终端按 Enter 继续；`--json` 只输出结果 JSON。默认 data-dir 为 `data/prompt-studio/`，prompt 目录为其中的 `prompts/`。
