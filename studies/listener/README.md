# 飞书会议旁听实验

独立于 `v2` 的最小可行性探测：**不调用 bot 入会接口**，以一个已在会议中的用户身份轮询飞书会议事件，把新字幕打印到终端标准输出（stdout）。无需安装第三方 Python 依赖；需 Python 3.11+。仅用于验证本租户是否能旁听和获取字幕，不发送消息、不持久化字幕或 token。

## 准备

1. 飞书自建应用开通**用户身份**权限 `vc:meeting.meetingevent:read`，按[官方旁听指南](https://open.feishu.cn/document/mcp_open_tools/agent-best-practices/agent-join-feishu-meetings)配置数据权限范围并发布。将 `http://127.0.0.1:8766/callback` 加入该应用的重定向 URL 白名单。
2. 用准备授权的**同一个用户**进入目标会议；会议 owner 开启「允许智能体入会」（找不到该选项时先开启「AI 汇总」）。旁听虽无可见 bot，这些授权和设置仍是前提。
3. 在终端设置该应用的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 环境变量；避免把凭据写入仓库或 shell 历史。若已有**相同应用、具备上述权限**的用户 access token，可只设置 `FEISHU_USER_ACCESS_TOKEN`，跳过本机授权。

从仓库根目录运行：

```bash
python3 studies/listener/listener.py --meeting-no 123456789
```

没有预设用户 token 时，程序会在 stderr 打印授权链接；在本机浏览器打开，使用正在参会的用户授权。OAuth 授权码和 access token 仅保留在本进程内，停止后下次需重新授权。省略 `--meeting-no` 时，用户必须恰好只参加一场会议；若参加多场，先运行一次查看终端报出的会议号，再指定其中一场。`--once` 只拉一次完整分页快照，`--interval 2` 可调整轮询周期；`--lookback 180` 控制每轮重叠回看的秒数。Ctrl-C 停止，输出可用终端重定向自行保存（内容含会议谈话及说话人）。

每条新句打印 `说话人: 字幕文本`。字幕由飞书服务端生成，不代表能即时到达；该模式官方仅支持轮询，不能订阅实时推送。实验采用内存去重（最多 20000 条），不保证长会、断网、token 到期后的完整性；运行中报 API 错误时退出。若报 `120002`，检查会议的 AI 汇总及允许智能体入会；若报 `120003`，检查授权用户是否仍在会中。程序不向会议发送会中文字。

API 依据：[旁听模式](https://open.feishu.cn/document/mcp_open_tools/agent-best-practices/agent-join-feishu-meetings)、[用户正在参加的会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/user_active_meeting.md)、[会议事件列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events.md)。
