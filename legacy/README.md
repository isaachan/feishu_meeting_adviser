# legacy · 方案 B 归档（本地 advisor + spool 文件交接）

本目录是方案 B 的实现与验证资产，**2026-11-10 起停止维护**，仅作历史参考与移植素材，不接收任何功能改动。现行架构（方案 C：云端 advisor + laptop 采集器）在仓库根目录，见根 [`README.md`](../README.md) 与 [`plan.md`](../plan.md)。

内容：`advisor/`、`local-meeting-recorder/`、`stt-server/`、`llm-gateway/`、`meeting-listener/`（停用）、`chatbot/`、`studies/`、`plan.md`、`TODO.md`、`docs/`、`integrations/`、`data/`（运行数据）、`assets/`。

仍在被现行模块引用的协议参考：[`docs/CONTRACT_STT_STREAM.md`](docs/CONTRACT_STT_STREAM.md)（方案 C 下 auth 固定 `none`）、[`docs/CONTRACT_REMOTE_INFERENCE.md`](docs/CONTRACT_REMOTE_INFERENCE.md)。

2026-11-13 仓库重组：本目录内容由仓库根目录迁入（`git mv`，历史可循）。
