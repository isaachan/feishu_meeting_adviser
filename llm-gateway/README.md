# llm-gateway

在服务器（当前基线：本机 localhost）运行 [远程推理契约](../legacy/docs/CONTRACT_REMOTE_INFERENCE.md)：接收 advisor 的推理任务，调度并隔离地执行 OpenCode（GLM），返回结构化决定。网关不解析会议语义，不保存长期会议状态。

- 首版**无认证**，仅限 localhost/可信内网（见 [plan.md](../plan.md) 已确认方向第 8 条；补认证已列入根 TODO）
- 后端：`fake`（确定性模拟）与 `opencode`（GLM，进程级隔离：每任务新实例、私有 XDG、guardian EOF 清理）。带组织者身份的任务仅开放 OpenCode `skill` 和受控的 `meeting_nsdp_lookup_release` 查询工具，其余工具拒绝。

## 运行

```bash
cd llm-gateway
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/llm-gateway serve --host 127.0.0.1 --port 8377 \
  [--database ../data/llm-gateway/tasks.sqlite3] \
  [--opencode-auth ~/.local/share/opencode/auth.json] \
  [--opencode-executable /usr/bin/opencode] \
  [--skills-dir /path/to/skills] [--auth-url http://127.0.0.1:8381] \
  [--noam-bin /path/to/noam]
```

默认内置 `nsdp-version` skill，用于查询版本和核对 Gate 日期；用 `--skills-dir` 可指定其他 skill 根目录（每个 skill 为 `<name>/SKILL.md`，将替代默认目录）。各 OpenCode 任务启动时读取配置，替换 skill 后新任务生效。gateway 所在主机需要安装 noam；查询时受控工具按任务组织者向 auth 按需读取 NSDP token，token 仅注入 noam 子进程。advisor 与 gateway 应同步部署，内部 API 仅在可信网络开放。

每场会议异步执行一次 `noam nsdp info` 并保存独立版本目录；推理任务固定提交时已就绪的目录，预热期间不等待、不自动补判。目录保存在任务数据库旁的 `<数据库文件名>.catalog.sqlite3`，默认按最后访问时间保留 24 小时。

启动输出一行 JSON（监听地址、数据库、恢复的任务数、pid）。Ctrl-C 干净停机：先停 HTTP，再停 worker，中断任务标记为 `failed(backend_unavailable)`，客户端按契约复用原 `request_id` 重试。

接口（详见契约）：

```text
POST /v1/inference-jobs                  提交（幂等：同 request_id 同 content_hash 返回原任务）
GET  /v1/inference-jobs/{request_id}     查询状态与结果（终态保留 24h 后过期清理）
POST /v1/inference-jobs/{request_id}/cancel
GET  /healthz
```

默认限额：全局并发 4、队列 64、期限上限 120s、载荷 2 MiB、保留 24h；均可由启动参数覆盖。

## 开发与测试

```bash
.venv/bin/ruff check src tests
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

默认测试使用合成输入与本地桩服务器（含 OpenCode 协议桩）。设置 `LIVE_OPENCODE_TEST=1` 时，`test_nsdp_integration.py` 额外使用真实 OpenCode 模型和合成 auth/noam 桩验证工具链路。
