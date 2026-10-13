# 阶段 1 自动化验证

日期：2026-10-13。状态：1.1–1.8 已实现并自动化验证，1.9 真实会议回归及 G1 尚未通过。

## 执行结果

从 `v2/` 执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s reference/spike/tests -v
```

产品测试 **65 项通过**，reference 测试 **36 项通过**。产品模块和测试语法编译通过；产品 wheel 可构建，包内没有 spike、reference 或测试代码。产品安装后的入口已在仓库之外的工作目录验证。

测试全部使用合成数据、假客户端、可控时钟或本机子进程，不请求真实飞书会议、不调用 LLM、不发送会中消息。`tests/fixtures/transcript_shapes.json` 依据已记录的实测字段形状和文档构造，值均为合成，不是从本机真实字幕生成的原始响应。

| 验证范围 | 证据 |
| --- | --- |
| 包安装、外部目录入口、reference 隔离 | `test_package.py` |
| RFC 3339 纳秒精度、时区、数字毫秒、未知时间 | `test_contract.py`、`test_diagnostics_retention.py` |
| 固定窗口分页、空页、游标循环、迟到回扫、缺字段 | `test_collector.py`、`test_pipeline.py` |
| 字幕、去重与检查点原子提交，容量失败回滚 | `test_storage.py`、`test_diagnostics_retention.py` |
| 消费持久化后确认，确认丢失后幂等重放 | `test_storage.py`、`test_process_recovery.py` |
| 超过原 spike 5000 键上限后的重启去重 | `test_pipeline.py`：5100 句，重启后首尾旧句不重复交付 |
| 无可靠版本的同秒变化、元数据修正、可信旧版重放 | `test_contract.py`、`test_collector.py`、`test_storage.py` |
| 人工选择候选后更新已有上下文，保留其他风险 | `test_pipeline.py` |
| HTTP/业务错误、401/403、限流、token 刷新、长断网 | `test_client_recovery.py` |
| 重复入离会、并发锁、session/配置变化、bot 离会 | `test_lifecycle.py` |
| 下游独立线程读取/确认 | `test_pipeline.py` |
| 日志轮转、降低上限、资料保留、删除副本、符号链接 | `test_diagnostics_retention.py` |
| 日志写盘失败后仍可取回已提交字幕、损坏记录不泄露正文 | `test_pipeline.py` |

## 进程级故障窗口

`tests/helpers/process_worker.py` 只供测试，所有 API 均替换为假客户端。

- 在字幕写入后、检查点写入后但事务未提交时，子进程直接 `os._exit()`：重启后整页回滚，检查点未推进。
- 在事务提交后、下游收到输入前直接退出：重启后已提交且未确认的输入可重放。
- 假消费者保存上下文后、确认源之前直接退出：重放被消费 ID 去重，上下文仍只有一条。
- 向运行真实产品 CLI 的子进程发送 SIGTERM：正常退出，bot session 保持 joined，未调用离会。
- 发送 SIGKILL：操作系统释放锁，新进程能启动并续采，已保存句子没有重复添加。

这验证的是进程故障与本机 SQLite 行为，不是硬件断电、网络盘或真实 API 稳定性保证。

## 实测前仍未覆盖

真实分页与跨进程游标有效性、留存边界、迟到事件的过滤时间语义、真实 token 到期/权限撤销、跨会议完整性、会议结束时实际响应、高事件量回扫对时延的影响，以及端到端回答效果均未通过真实验收。疑似缺口保持显式状态；模拟恢复成功不能将其自动标为完整。

进入 1.9 需要两场新增、已获参会者同意的测试会议，确认当前会议设置和本机配置，并人工配合发言、时间标注及断网/重启操作。真实回归步骤见 [STAGE1_MEETING_REGRESSION.md](STAGE1_MEETING_REGRESSION.md)。仅记录脱敏统计；在取得真实证据前保持 1.9 与 G1 未完成。

## 实测辅助入口补充

新增 `transcripts --show-text [--limit 20]`，仅预览当前保存会议已接受的字幕，不投递、不确认、不请求飞书；默认隐藏正文与姓名。补充 3 项测试验证权限开关、已消费记录仍可查看、会议隔离、条数边界以及采集/消费进度不变；当前累计 68 个产品测试通过。真实字幕确认仍需在本机与测试发言对照。

### 采集实时输出补充

新增 `collect --show-text`，每页提交后立即打印首次入库字幕和修订/冲突候选，默认关闭。包含说话人、接收时间及上游时间、正文、变更类型和扫描模式，不改变消费进度。终端输出是即时观察入口；若提交后进程退出或输出失败，已落盘内容仍保留，可用 `transcripts` / `conflicts` 核对，重启不重放已入库字幕到终端。

新增 5 项测试验证默认隐藏内容、跨页即时刷新与先入库后输出、重启去重且不确认消费、冲突候选标记、非法页不输出未提交内容、输出失败后字幕与检查点仍保留。当前累计 73 个产品测试通过；全部使用临时目录与合成字幕，不操作真实会议。1.9 和 G1 仍待真实会议回归。
