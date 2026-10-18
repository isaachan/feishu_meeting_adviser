# 飞书聊天机器人

## 准备

在飞书开发者后台创建自建应用，启用机器人，订阅 `im.message.receive_v1` 事件并选择**长连接**。开通接收单聊消息、接收群内 @ 消息和以机器人身份发送消息的权限，然后发布应用。

```bash
cd chatbot
python3 -m venv .venv
.venv/bin/python -m pip install -e .
test -f config.yaml || cp config.example.yaml config.yaml
```

填写 `config.yaml` 里的飞书 `app_id`、`app_secret` 和 OpenCode 的 `model`。模型凭据二选一：填写 `api_key`，或在 `auth_file` 填写本机 OpenCode 登录文件的绝对路径（通常为 `/Users/<用户名>/.local/share/opencode/auth.json`）。后者只读取当前模型 provider 的 API 登录，不复制密钥到 YAML。应用配置只来自 YAML 和命令行，不从环境变量读取；`auth_file` 是 YAML 指定的凭据来源。`config.yaml` 已加入 Git 忽略列表。把 UTF-8 的 `.txt`、`.md`、`.yaml`、`.yml`、`.json` 文件放进 `context/`；隐藏文件、符号链接和其他格式会跳过。

GLM Coding Plan 的[使用政策](https://docs.z.ai/devpack/usage-policy)禁止多人共享订阅权益，并把使用范围限定在官方支持的工具和场景。供多人使用的飞书机器人应使用适用的独立 API 服务；`v2` 使用 OpenCode 登录并不自动说明这种聊天机器人用法符合订阅条款。

```bash
mkdir -p context
.venv/bin/python -m chatbot --context context --backend opencode
```

机器人回复单聊文本和群聊中 @ 它的文本消息。每个群或单聊保留最近 `history_turns` 轮上下文，仅在内存中保存，重启后清空。背景资料每轮重新读取。OpenCode 每轮使用独立临时目录，工具权限关闭，运行结束后删除临时文件。
