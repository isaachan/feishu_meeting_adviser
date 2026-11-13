# 远程服务器开发与部署（Remote Server Development）

> **本文档已停维（方案 B 部署基线）。** 其"advisor 与 recorder 始终在 laptop 本地运行、仅需远程部署 stt-server 和 llm-gateway"的结论**已过时**：方案 C（2026-11-10 立项）中 advisor、bot、auth、stt-server、llm-gateway 五个节点全部部署在云端服务器，laptop 只跑 recorder。现行部署基线见 [new/docs/DEPLOYMENT.md](../new/docs/DEPLOYMENT.md)，开发入口为 [new/README.md](../new/README.md)。本文仅作历史参考（M2/5.11 部署经验仍可借鉴）。

状态：2026-11-10 建立。汇总远程服务器上两个服务模块的部署、客户端接入与验证方式；对应里程碑 M2（llm-gateway 远程真机验证，2026-11-09）、5.11 远程 STT 验收（2026-11-10）与飞书组织者授权验收（2026-11-10）。

## 1. 模块与运行位置

| 模块 | 位置 | 角色 |
|---|---|---|
| `stt-server/` | **远程服务器** | 流式 STT 服务（sherpa-onnx，WebSocket 契约 [CONTRACT_STT_STREAM](CONTRACT_STT_STREAM.md) v2） |
| `llm-gateway/` | **远程服务器** | 模型执行网关（OpenCode/GLM，HTTP 契约 [CONTRACT_REMOTE_INFERENCE](CONTRACT_REMOTE_INFERENCE.md)） |
| `advisor/` | 用户 laptop | 会议会话与决策（经契约调用 llm-gateway） |
| `local-meeting-recorder/` | 用户 laptop（Mac） | 音频采集客户端（默认 `stt: remote-stream` 调 stt-server） |
| `meeting-listener/` | — | 已停用（存档，见 plan.md） |

即：**需要部署到远程服务器的只有 `stt-server` 和 `llm-gateway` 两个模块**，二者并列部署、互相独立；advisor 与 recorder 始终在 laptop 本地运行。

当前验证环境：内网 Ubuntu Server（CPU），laptop 侧示例地址 `http://10.132.67.11:8377`（见 `advisor/config.test.yaml`）。两服务首版均**无认证**，仅限可信内网（plan.md 已确认方向第 8、10 条；公网/多用户前必须补认证与 TLS，根 TODO 已列）。

## 2. 端口一览

| 端口 | 服务 | 协议 | 开放范围 |
|---|---|---|---|
| 8377 | llm-gateway | HTTP | 内网（laptop advisor 访问） |
| 8392 | stt-server | WebSocket | 内网（laptop recorder 访问） |
| 8393 | stt-server healthz | HTTP | 建议仅本机/运维网段 |

## 3. stt-server 部署

安装与模型下载（SHA-256 校验）见 [stt-server/README.md](../stt-server/README.md)，要点：

```bash
cd stt-server
python3 -m venv .venv && .venv/bin/python -m pip install -e .
# 下载 sherpa-onnx 流式 Zipformer（zh-en-punct int8，约 128 MiB，不进 Git）
.venv/bin/stt-server serve --host 0.0.0.0 --port 8392 \
  --model-dir models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05 \
  --threads 2 --max-sessions 8 --auth feishu
curl http://127.0.0.1:8393/healthz   # {"status":"ok","model":…,"sessions":N,"auth":"feishu"}
```

- `--auth feishu`（契约 v2）：建连时用客户端携带的 user_access_token 调飞书 Open API 核验**组织者身份 + [−30, +30] min 会议时间窗**，fail-closed（飞书不可达 → `auth_unavailable` 拒绝）。**服务端不需要飞书 app 凭据**（app_id/secret 只在 recorder 侧 OAuth 使用）；服务器只需出网访问 `open.feishu.cn`。通过结果缓存 10 min，避免 mic/loopback 双连接重复核验。
- `--auth none` 仅保留给 localhost 开发基线。
- 规模参考：每客户端 2 连接（mic + loopback）；`--max-sessions` ≥ 在线客户端数 × 2；int8 模型每会话 RTF ≈ 0.1（2 线程），8 会话约需 2–4 核富余。
- systemd 单元示例见模块 README。

## 4. llm-gateway 部署

见 [llm-gateway/README.md](../llm-gateway/README.md)，要点：

```bash
cd llm-gateway
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
.venv/bin/llm-gateway serve --host 0.0.0.0 --port 8377 \
  --database ../data/llm-gateway/tasks.sqlite3 \
  --opencode-auth ~/.local/share/opencode/auth.json \
  --opencode-executable /usr/bin/opencode
curl http://127.0.0.1:8377/healthz
```

- 依赖服务器上已安装 **OpenCode**（系统级安装时必须显式 `--opencode-executable`，默认路径常不匹配——M2 实测踩坑，见第 7 节）与 Coding Plan 凭据 `auth.json`；每任务独立 OpenCode 实例（随机密码、私有 XDG、deny-all、guardian 清理），服务器需出网访问模型提供方。
- 默认限额：全局并发 4、队列 64、期限上限 120s、载荷 2 MiB、结果保留 24h；均可由启动参数覆盖。
- 干净停机（Ctrl-C / SIGTERM）：先停 HTTP 再停 worker，非终态任务标记 `failed(backend_unavailable)`，客户端复用原 `request_id` 重试；重启后自动恢复任务记录。
- 数据库与任务结果目录需持久化（勿放 tmpfs）。

## 5. 客户端接入（laptop）

**advisor**（`config.yaml`）：

```yaml
llm:
  backend: remote
  remote_url: http://10.132.67.11:8377   # 网关地址
  remote_backend: opencode               # 或 fake（测试）
```

**local-meeting-recorder**（real 模式默认 `--stt remote-stream`，须显式给地址，否则启动报错）：

```bash
.venv/bin/recorder auth login                        # 飞书 OAuth（LMR_FEISHU_APP_ID/SECRET）
.venv/bin/recorder meetings                          # 列出 ±30min 窗口内会议（[组织者] 才可用）
.venv/bin/recorder real --session <session_id> --mic \
  --stt-url ws://10.132.67.11:8392 --event '<calendar_id>/<event_id>'
```

- `--stt-url` 或 `LMR_STT_URL` 指定服务地址；`--event` 随流写入 spool meta 并作为 `start.auth` 供服务端核验；`auth_expired` 客户端静默刷新重连。
- 断线即新会话（偏移重计），客户端本地始终留存整场音频，服务端重启不影响采集（契约第 4 节）。

## 6. 验证

- 两个 `/healthz` 先行；stt-server healthz 可确认 `auth` 模式与当前会话数。
- 端到端：recorder real → 字幕落 spool → advisor 消费 → `llm.backend: remote` 决策落库（`replies` 查看）。
- 既有验收证据：M2 远程全链路（plan.md 里程碑节）；5.11 远程 STT（事件→落库 149–296ms，`data/acceptance-5-6/`）；组织者授权（`data/acceptance-auth/`，非组织者/未知事件/无效 token 三类拒绝均验证）。

## 7. 已知部署问题记录

- **opencode 系统安装路径不匹配**（2026-11-09，M2）：系统级安装的 opencode 不在默认查找路径，需 `--opencode-executable /usr/bin/opencode` 显式指定。
- **`/provider` 慢目录**（2026-11-09，M2）：OpenCode 校验端点 `/provider` 可能拉取远端目录超过 1s 预算，网关已把单请求预算放宽到 10s（整体仍受 deadline 看门狗约束）。

## 8. 待办边界（引用）

- 网关用户认证与按用户限额（根 TODO；启用后更新远程推理契约第 2 节）。
- 阶段 6.1：TLS 接入、受控凭据注入、进程生命周期与有界停机的正式部署规范。
- 组织者授权的已知残留风险：所核验会议与实际音频来源无强绑定（TODO 3.8 之后的 hardening 项，见 d510065 提交记录）。
