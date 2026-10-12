# v2 阶段 0：授权和会中事件探测

[计划](PLAN_OFFICIAL_MEETING_EVENTS.md) · [TODO](docs/TODO.md)

`scripts/stage0.py` 是**单次、有界、只读的探测工具**，不是会中轮询服务；尚未获得真实会议验证。OAuth 路径使用候选的飞书 v1 用户授权接口，会议接口/响应结构依据员工线索；使用前先完成 TODO 0.1 的**官方文档及应用控制台核对**，如契约不同应调整代码，不能把当前实现当作官方保证。必须在获同意的测试会议中使用。

## 配置（唯一凭据来源）

```bash
cd v2
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

所有需用户提供的数据（`app.id`、`app.secret`、`oauth.redirect_uri`、OAuth 回调的 `code` / `returned_state`、已有的 `user_access_token` / `refresh_token`）**只填写 `v2/config.yaml`**。脚本不读取环境变量、不从 CLI 参数接受凭据；该文件及 `.venv/` 被 `v2/.gitignore` 忽略。不要提交配置、复制原始事件响应、在工单/聊天中发送 token 或未经同意的会议内容。

1. 在开放平台建立测试应用，配置 HTTPS 重定向地址并开通/授权 `vc:meeting.meetingevent:read`（以控制台和官方契约为准）。把 App ID、App Secret 和重定向地址填入 `config.yaml`。
2. 执行 `.venv/bin/python scripts/stage0.py auth-url`：本地生成并写入随机 `oauth.state`，输出授权 URL。用**要参会的用户**打开并授权。将回调 URL 中的 `code` 和 `state` 分别填到 `config.yaml` 的 `oauth.code`、`oauth.returned_state`，不要将含 code 的完整回调 URL 发到终端命令或他人处。
3. 执行 `.venv/bin/python scripts/stage0.py exchange`：校验 state，获取用户 token 并写回 `config.yaml`，清除 code。过期时执行 `.venv/bin/python scripts/stage0.py refresh`；如已有合法用户 token，也可只填 `oauth.user_access_token`，跳过前两步。交换或刷新失败不会输出响应原文或 token；核对文档/权限后再尝试。
4. 用户加入获授权测试会议、确认相关开关后执行 `.venv/bin/python scripts/stage0.py probe`。仅输出是否找到会议、meeting ID 位数、受限页数内的事件**计数和固定字段类型**，不输出字幕正文、发言人、ID、游标或原始响应。`probe.max_pages` 最多 10 页；不会持续轮询或落盘事件。没有 `transcript_received` 不能独立断言接口不支持；需核对开关、权限及实际响应契约。
5. 安排第二个参会账号在会中说话，按 [TODO](docs/TODO.md) 记录成功/失败（不含隐私文本）；用未入会、无开关、无权限、过期 token、切会等测试账号/场景**分别**做人工验证，不要为了负例修改真实会议安全设置而不告知他人。

如输出“response lacks data object”或未找到会议，说明当前实现的响应结构尚未匹配真实接口；请先核验官方契约、在私有安全环境检查实际响应结构，**不要直接把含会议内容的响应粘贴到仓库**。真实接口尚未实测，OAuth 有效期、事件字段及空页游标行为均不作保证。脚本测试：`.venv/bin/python -m unittest discover -s tests -v`。
