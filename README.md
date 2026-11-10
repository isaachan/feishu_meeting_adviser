# 会参谋（新架构）

**本仓库根目录下除 `new/` 外的所有内容已停止维护**（advisor、local-meeting-recorder、llm-gateway、stt-server、chatbot、meeting-listener、plan.md 及各契约文档）。它们是旧架构（方案 B：本地 advisor + 远程模型执行）的实现与验证资产，仅作历史参考与移植素材保留，不再接收功能改动。

**一切新开发都在 [`new/`](new/) 进行。**

- 产品需求与场景：[new/docs/REQUIREMENTS.md](new/docs/REQUIREMENTS.md)
- 架构设计（方案 C：本地采集 + 云端 advisor）：[new/docs/ARCHITECTURE.md](new/docs/ARCHITECTURE.md)
- 部署节点与基线：[new/docs/DEPLOYMENT.md](new/docs/DEPLOYMENT.md)

新架构一句话：laptop 只装飞书和采集器（录音上行），会话状态、字幕、模型决策、通知 bot 全部集中在云端服务器，天然支持"组织者授权、参会者订阅"的多用户场景。
