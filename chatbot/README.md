# 飞书聊天机器人

## 准备

在飞书开发者后台创建自建应用，启用机器人，订阅 `im.message.receive_v1` 事件并选择**长连接**。开通接收单聊消息、接收群内 @ 消息和以机器人身份发送消息的权限，然后发布应用。

```bash
cd chatbot
python3 -m venv .venv
.venv/bin/python -m pip install -e .
test -f config.yaml || cp config.example.yaml config.yaml
```

填写 `config.yaml` 里的飞书 `app_id`、`app_secret`，以及 OpenCode 的 `model` 和相应 provider 的 `api_key`。`config.yaml` 已加入 Git 忽略列表，`config.example.yaml` 是不含密钥的示例。应用只从配置文件和命令行读取配置，不使用环境变量或 OpenCode 本机登录。把 UTF-8 的 `.txt`、`.md`、`.yaml`、`.yml`、`.json` 文件放进 `context/`；隐藏文件、符号链接和其他格式会跳过。

```bash
mkdir -p context
.venv/bin/python -m chatbot --context context --backend opencode
```

机器人回复单聊文本和群聊中 @ 它的文本消息。每个群或单聊保留最近 `history_turns` 轮上下文，仅在内存中保存，重启后清空。背景资料每轮重新读取。OpenCode 每轮使用独立临时目录，工具权限关闭，运行结束后删除临时文件。
