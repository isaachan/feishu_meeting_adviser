# release-2026.11.26-1701

- 仓库 tag：`release-2026.11.26-1701`
- 时间：2026-11-26T17:14:50+08:00
- Commit：`ac8c95d`
- 上一仓库 tag：无（首个 tag）

## 组件版本

| 模块 | 版本 | 本次是否变更 |
|---|---|---|
| admin-site | 0.1.1 | 首个 tag，基线 |
| advisor | 0.1.0 | 首个 tag，基线 |
| auth | 0.1.0 | 首个 tag，基线 |
| bot | 0.1.0 | 首个 tag，基线 |
| gui | 0.1.0 | 首个 tag，基线 |
| harness | 0.1.0 | 首个 tag，基线 |
| llm-gateway | 0.1.1 | 首个 tag，基线 |
| recorder | 0.1.0 | 首个 tag，基线 |
| stt-server | 0.1.0 | 首个 tag，基线 |

## 变更

首个仓库 tag，以下为当前交付能力基线总结，不逐条罗列历史 commit。

### 新功能

- 会参谋（方案 C）实时会议 AI 助理整体架构：组织者听会、判断并答复，经组织者授权向参会者分发。
- 云端 advisor 会话核心：多租户会话域、recorder ingest 网关、会话监督器、订阅/审批/出站（outbox）、NSDP 材料取数与 owner 作用域缓存。
- bot 独立节点：飞书 IM 控制面（会议列表、入会、结束/重新入会、订阅审批、状态查询、帮助），Jira 工单号渲染链接。
- auth 节点：多用户 OAuth 凭据托管、单飞刷新、身份档案与 glossary 目录、GUI device-flow 登录。
- recorder 采集器：Mac/Windows 双路采集、飞书静音门控、WS 上行重传窗口与会话 drain 语义。
- stt-server：sherpa 流式与 FireRedASR2 VAD 分段离线引擎，纯内网服务化。
- llm-gateway：模型执行网关、会话语义快照、工具链（NSDP/Jira 只读）、耗时阶段插桩。
- gui 桌面客户端：登录/会话发现/采集监控，单会话连接监视。
- admin-site 运维台：会话列表与手工停止、日志 SSE tail、运行时配置、文字记录导出。

### 修复

- 会话结束后可重新入会、draining 期间拒绝重复建会（schema v12 放宽实例唯一性）。
- 响应式文字回复（Feishu rich-text/card）与 Markdown 渲染回退。
- 多项稳定性修复：模型单次结果异常不再拖死会话、GUI 网络失败兜底、飞书代理连接、日历循环日程解析、Windows 麦克风 COM 初始化等。

### 重构 / 测试 / 文档

- 统一各模块版本来源为 `pyproject.toml`。
- 删除 legacy（方案 B）历史资产，macos-gui 更名为 gui。
- 契约文档体系（ARCHITECTURE/REQUIREMENTS/STATE_MACHINES 及各模块 CONTRACT_*）、部署与本地测试文档齐备。
- 跨模块集成测试床（integrations）与各模块单测/静态检查；CI 覆盖 gui/integration 作业环境。

### 其他

- 新增 `release-tag` 技能与 `.agents/skills` 注册，明确版本打标流程。

## 升级与迁移

- 首个 tag，无历史 tag 升级约束。
- advisor 数据库经历了 schema 演进（最新 v12），非预发布环境需按当前 schema 初始化/迁移。
- 运行时配置与凭据边界见 `docs/DEPLOYMENT.md`。

## 部署与回滚

- 本 tag 只完成打标，不包含生产部署。
- 部署前需在 server 确认 CI 绿，再按 `docs/DEPLOYMENT.md` §7.2 手动执行。
- 回滚点：无上一 tag（首个仓库 tag）。

## 本地验证

- 测试命令：见 `.agents/skills/release-tag/SKILL.md` 第 5 步（各模块 `ruff` + `pyright` + `unittest`，以及 `integrations` 集成测试）。
- 结果：见本次打标交接摘要，以实际执行结果为准。
- CI 未作为打 tag 门槛；部署前需在 server 确认 CI 绿。
