# v2：产品级飞书字幕输入源

[计划](PLAN_OFFICIAL_MEETING_EVENTS.md) · [TODO](docs/TODO.md) · [Spike 实验与复现](reference/spike/README.md)

当前唯一路线是应用身份 `tenant_access_token` → `bots/join` → `bots/events`。接口可行性和单场连续采集已试通，证据见 [阶段 0 实测笔记](docs/STAGE0_FINDINGS.md)。产品正在按阶段 1 重新实现；历史代码和测试已隔离到 `reference/spike/`，产品不导入它们。真实问答尚未实现，20 秒端到端目标仍待阶段 2 验收，不回退 v1。

## 目录与安装

- `src/feishu_meeting_adviser/`：产品 Python 包。
- `tests/`：产品测试。
- `reference/spike/`：旧实验代码、测试、配置示例及复现说明。
- `docs/`：接口观察、计划与脱敏测试记录。

从 `v2/` 执行（Python 3.11+）：

```bash
python3 -m venv .venv  # 已有环境可直接使用
.venv/bin/python -m pip install -e .
.venv/bin/python -m feishu_meeting_adviser --help
.venv/bin/python -m unittest discover -s tests -v
```

产品配置入口为本机 `v2/config.yaml`，运行数据将保存在 `v2/data/`；当前入口提供帮助和版本信息，业务命令随实现加入。已有 spike 配置、session 和输出保持原位，勿覆盖配置或自动导入旧状态；正式配置及迁移说明随产品配置实现补充。真实资料、凭据、session 和运行数据均不提交仓库。

## Spike 复现

从 `v2/` 执行：

```bash
.venv/bin/python -m pip install -r reference/spike/requirements.txt
.venv/bin/python -m unittest discover -s reference/spike/tests -v
.venv/bin/python reference/spike/scripts/stage0.py --help
```

真实会议操作见 [Spike README](reference/spike/README.md)，仍从本机 `v2/config.yaml` 读取凭据。复现实验需参会者同意；迁移没有复制真实配置、session 或字幕。实验验证和产品验证分别记录。
