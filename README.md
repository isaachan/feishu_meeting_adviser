# 飞书实时会议 AI 助理

项目分为两个独立模块，通过本机 SQLite 数据交接：

- **[advisor](advisor/README.md)**：本地会议会话、消费字幕、会议上下文、模型决策与本地结果保存，包含本地 harness；经 `llm.backend: remote` 调用网关。
- **[meeting-listener](meeting-listener/README.md)**：**已停用**（飞书权限限制）。代码与测试保留，不再处于默认运行路径；归档范围在 [plan.md](plan.md) 阶段 6 核对。
- **[data](data/README.md)**：本地会话、字幕、消费进度、模型任务及运行日志。
- **[llm-gateway](llm-gateway/README.md)**：localhost 模型执行网关（仅 OpenCode/GLM，首版无认证、限内网）。
- **[local-meeting-recorder](local-meeting-recorder/README.md)**：fake 台词回放（真实声卡采集暂缓）。
- **[plan.md](plan.md)**：方案 B（本地 advisor + 远程模型执行）的架构、契约与任务清单。

advisor、`local-meeting-recorder` 与 `llm-gateway` 各自维护配置、环境、测试和 `pyproject.toml`。根目录 `integrations/` 默认全部是集成测试。

## 安装

从仓库根目录执行，要求 Python 3.11+：

```bash
python3 -m venv advisor/.venv
advisor/.venv/bin/python -m pip install -e './advisor[dev]'
```

仅在模块还没有本机 `config.yaml` 时，复制 `config.example.yaml` 并填写凭据，设置文件权限为 `600`。`storage.directory` 默认指向 `../data`。模型后端在本地以 OpenCode（GLM）或 Codex 运行；远程 `llm-gateway` 按计划在阶段 3–4 接入。

## 运行（本地会话）

```bash
cd advisor
.venv/bin/python -m feishu_meeting_adviser session-new --title 周会
.venv/bin/python -m feishu_meeting_adviser consume --watch <session_id>
# 停止输入并收尾
.venv/bin/python -m feishu_meeting_adviser session-end <session_id>
```

会话由 advisor 显式创建，不依赖飞书会议号或 bot 入会；模型回答保存为本地结果（`replies` 查看），不发送任何通知（notification 搁置，见 plan.md）。字幕由 fake-recorder 经[交接契约](docs/CONTRACT_RECORDER_INPUT.md)进入会话；模型调用经[远程推理契约](docs/CONTRACT_REMOTE_INFERENCE.md)发往网关（`llm.backend: remote`）。M1（fake-recorder → advisor → 网关假模型 → 本地有效决定）已由集成测试打通；真实 OpenCode 验证（M2）待做。

## 文档与测试

- 各模块的运行说明、单模块文档和测试见各自目录；跨模块规划与契约见 [plan.md](plan.md)。
- [飞书权限清单](docs/APP_PERMISSIONS_EVENTS.md)、[架构评审](docs/2026-10-15-architecture-review.md)、[实现记录](docs/IMPLEMENTATION_RECORD.md)。
- [数据操作](data/docs/DATA_OPERATIONS.md)说明保存、清理、授权与恢复边界。
- [集成测试说明](integrations/README.md)。

方案 B 的目标是在会议进行时获取内容并提供辅助，模型执行集中在服务端网关。旧的 20 秒 P95 目标来自飞书字幕路线，本地采集 + 远程执行的时延预算将在 M1 全链路后重新测量。

独立可行性研究位于 [studies/local-meeting](studies/local-meeting/README.md) 和 [studies/listener](studies/listener/README.md)；官方接口实验位于 `meeting-listener/reference/spike/`。
