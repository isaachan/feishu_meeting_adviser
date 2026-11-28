# release-2026.11.28-2222

- 仓库 tag：`release-2026.11.28-2222`
- 时间：2026-11-28T22:23:07+08:00
- Commit：`4849d75`
- 上一仓库 tag：`release-2026.11.26-1701`

## 组件版本

| 模块 | 版本 | 本次是否变更 |
|---|---|---|
| admin-site | 0.1.2 | 是（0.1.1 → 0.1.2） |
| advisor | 0.1.1 | 是（0.1.0 → 0.1.1） |
| auth | 0.1.1 | 是（0.1.0 → 0.1.1） |
| bot | 0.1.0 | 否 |
| gui | 0.1.1 | 是（0.1.0 → 0.1.1） |
| harness | 0.1.0 | 否 |
| llm-gateway | 0.1.2 | 是（0.1.1 → 0.1.2） |
| prompt-studio | 0.1.0 | 新增模块 |
| recorder | 0.1.0 | 否 |
| stt-server | 0.1.0 | 否 |

## 变更

本 tag 覆盖 `release-2026.11.26-1701..HEAD` 的改动（27 个提交）。

### 新功能

- 新增 `prompt-studio` 模块：本地提示词测试平台，含 Web UI 与 `serve` 命令、CLI `init/replay/submit-tokens`、Stack 编排与 replay、脚本化 fake STT 与 ingest 客户端、TXT 解析与 material 注入、飞书文档抓取到场景、显式绑定 `0.0.0.0` 供内网访问。
- advisor：解析会议描述中的飞书 docx/wiki 并抓取正文并入材料，`create_session` 支持文档材料与 partial 状态，缺 scope 时降级提示。
- auth：新增 docx/wiki 只读 scope 用于会议文档材料；新增 `admin:login` 发现式登录变体，回调 302 返回 admin 站点。
- admin-site：强制飞书 OAuth 登录并校验管理员白名单（`ADMIN_USERS`），展示当前登录用户并支持登出；脚本接入 `/admin` 前缀。
- gui：最小化收进系统托盘并显示连接状态。
- llm-gateway：支持 `--prompt-dir`，在每次决策时读取提示词。
- assets：恢复项目图标 SVG 与 1024 PNG。

### 修复

- auth：admin 登录回调 URL 可配置，适配本地开发。

### 重构 / 测试 / 文档

- prompt-studio：接入 CI、补充使用手册与隔离部署文档，说明必须配置 opencode provider，用 paced 模式替代 step，移除 CLI fake 后端并更新默认模型，传递 Jira URL 到 gateway。
- docs：同步建会文档材料与新增 scope 口径。

### 其他

- admin-site 相关脚本（`scripts/`）与 `.github/` CI 配置更新。

## 升级与迁移

- 新增 auth 配置项：`ADMIN_USERS` 为 admin-site 登录白名单，未配置时 admin-site 不可用。
- auth OAuth 增加 docx/wiki 只读 scope，需在飞书应用侧确认已授权相应权限，否则会议文档抓取降级。
- 新增 `prompt-studio` 模块（可选开发工具），不影响线上服务。
- 其余模块无数据库 schema 变更。

## 部署与回滚

- 本 tag 只完成打标，不包含生产部署。
- 部署前需在 server 确认 CI 绿，再按 `docs/DEPLOYMENT.md` §7.2 手动执行。
- 回滚点：上一仓库 tag `release-2026.11.26-1701`。

## 本地验证

- 测试命令：见 `.agents/skills/release-tag/SKILL.md` 第 5 步（各模块 `ruff` + `pyright` + `unittest`，以及 `integrations` 集成测试；`prompt-studio` 纳入同批检查）。
- 结果：见本次打标交接摘要，以实际执行结果为准。
- CI 未作为打 tag 门槛；部署前需在 server 确认 CI 绿。
