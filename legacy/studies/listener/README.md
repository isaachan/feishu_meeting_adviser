# 飞书会议旁听实验（研究已放弃）

> **2026-11-09 正式放弃旁听模式研究。** 链路已实测验证到最后一环，确认当前不可行；证据见下。工具本身保留：若账号/企业日后进入「允许智能体入会」白名单，可按下文直接复测。

独立于产品模块的最小可行性探测：**不调用 bot 入会接口**，以一个已在会议中的用户身份轮询飞书会议事件，把新字幕打印到终端标准输出（stdout）。无需安装第三方 Python 依赖；需 Python 3.11+。不发送消息、不持久化字幕或 token。

## 放弃结论与实测证据（2026-11-09）

- **已打通**：应用用户身份权限 `vc:meeting.meetingevent:read` 授予正常（授权 token 无缺 scope 警告）；本机 PKCE 授权与换 token 成功——注意 authen v2 换 token 响应的 `access_token`/`scope` 位于**顶层**而非 `data` 内；`user_active_meeting` 正常返回会议号、标题与内部会议 ID，该接口不受会议门禁限制。
- **唯一阻塞**：`bots/events` 持续返回 HTTP 403 / code=120002。官方口径：需会议由 owner 开启「AI 汇总」与「允许智能体入会」，而后者**仅对白名单账号可见**；本账号不在白名单，自己主持的会议无法解除门禁。实测确认 120002 是硬门禁：程序忽略该错误持续轮询，也拿不到任何事件数据。
- **决定**：不加白则无会中字幕；加白周期不可控，研究停止，转替代方向。

## 替代方向

| 需求 | 方向 | 备注 |
|---|---|---|
| 会后转写 | 妙记公开 API（转写导出、媒体下载） | 无需白名单，仅需会议开启 AI 汇总；接口清单见[共享内容调研](../../advisor/docs/SCREEN_SHARE_CONTENT_INVESTIGATION.md) |
| 会中实时 | 申请白名单（飞书技术支持/早鸟体验群），或由白名单账号担任 owner | 开关按会议生效；打通后用本工具复测 |
| 会中实时且不依赖飞书权限 | 本地录制 + ASR | 无说话人分离等已知缺点 |

## 工具用法（复测时用）

1. 飞书自建应用开通**用户身份**权限 `vc:meeting.meetingevent:read`，按[官方旁听指南](https://open.feishu.cn/document/mcp_open_tools/agent-best-practices/agent-join-feishu-meetings)配置数据权限范围并发布。将 `http://127.0.0.1:8766/callback` 加入该应用的重定向 URL 白名单。
2. 用准备授权的**同一个用户**进入目标会议；会议 owner（须在白名单内）开启「允许智能体入会」（找不到该选项时先开启「AI 汇总」）。
3. 在终端设置该应用的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 环境变量；避免把凭据写入仓库或 shell 历史。若已有**相同应用、具备上述权限**的用户 access token，可只设置 `FEISHU_USER_ACCESS_TOKEN`，跳过本机授权。

从仓库根目录运行：

```bash
python3 studies/listener/listener.py --meeting-no 123456789
```

没有预设用户 token 时，程序会在 stderr 打印授权链接；在本机浏览器打开，使用正在参会的用户授权。OAuth 授权码和 access token 仅保留在本进程内，停止后下次需重新授权。省略 `--meeting-no` 时，用户必须恰好只参加一场会议；若参加多场，先运行一次查看终端报出的会议号，再指定其中一场。`--once` 只拉一次完整分页快照，`--interval 2` 可调整轮询周期；`--lookback 180` 控制每轮重叠回看的秒数。Ctrl-C 停止，输出可用终端重定向自行保存（内容含会议谈话及说话人）。

每条新句打印 `说话人: 字幕文本`。字幕由飞书服务端生成，不代表能即时到达；该模式官方仅支持轮询，不能订阅实时推送。实验采用内存去重（最多 20000 条），不保证长会、断网、token 到期后的完整性。遇会议门禁 120002 时程序不退出，每隔约 30 秒在 stderr 提示并持续重试——本次实测证明重试不会带来数据，仅用于观察门禁变化；网络瞬断按 5 秒退避重试，连续 30 次才放弃，其余 API 错误立即退出。每轮拉取在 stderr 打印事件计数（transcript/chat/join/left/other），stdout 保持纯字幕。若报 `120003`，检查授权用户是否仍在会中。程序不向会议发送会中文字。

API 依据：[旁听模式](https://open.feishu.cn/document/mcp_open_tools/agent-best-practices/agent-join-feishu-meetings)、[用户正在参加的会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/user_active_meeting.md)、[会议事件列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events.md)。
