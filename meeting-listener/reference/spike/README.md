# 官方会议接口实验

仅用于复现实验。产品运行不导入此处代码；单场实验不能替代可靠性、真实分页或端到端性能验收。

## 配置和数据

从 `meeting-listener/` 执行，使用本模块的 `.venv`。实验使用独立的旧接口探测配置结构，不读取产品 `config.yaml`。

```bash
# 只在尚无实验配置时执行
mkdir -p ../data/spike
cp reference/spike/config.example.yaml ../data/spike/config.yaml
chmod 600 ../data/spike/config.yaml
```

填写实验配置的 app 凭据、`lookup.meeting_no` 和可选 `join.password`。会议号为带引号的九位字符串。配置、入会会话和输出分别位于仓库根目录的 `data/spike/config.yaml`、`data/spike/session.yaml`、`data/spike/output/`，不提交仓库。

## 操作

```bash
.venv/bin/python reference/spike/scripts/stage0.py join
.venv/bin/python reference/spike/scripts/stage0.py probe
.venv/bin/python reference/spike/scripts/stage0.py transcripts
.venv/bin/python reference/spike/scripts/stream_transcripts.py
.venv/bin/python reference/spike/scripts/stage0.py send-message "合成测试文字"
.venv/bin/python reference/spike/scripts/stage0.py leave
.venv/bin/python -m unittest discover -s reference/spike/tests -v
```

`join` 以应用身份入会，从响应保存内部会议 ID；`probe` 只显示事件类型与字段类型；`transcripts` 单次读取，`stream_transcripts.py` 持续采集并去重。Ctrl-C 停止轮询，不离会。API 仍受权限、灰度和主持人开启 AI 汇总等前提约束。

`send-message` 仅在显式执行时发一次会中文本，不提供语音。省略文字时使用固定测试文案；超时或返回异常先核对会议界面，不盲目重发。可配置固定 `message.uuid`。`diagnose` 可显示本机脱敏诊断，服务端 msg 仍仅供本机核查。

输入可靠性和时延证据见 [阶段 0 实测](../../docs/STAGE0_FINDINGS.md)、[字幕延迟调研](../../docs/TRANSCRIPT_LATENCY_INVESTIGATION.md)；当前产品操作见 [meeting-listener](../../README.md)，联合规划见 [总体计划](../../../docs/PLAN_OFFICIAL_MEETING_EVENTS.md)。
