# 本地 LLM harness

`meeting_adviser_harness` 用本地材料和模拟字幕运行产品的 input → decision → reply 循环。它是独立包，依赖 `feishu_meeting_adviser` 的生产实现；生产包不依赖 harness。

## 启动

在 `v2/` 和已安装项目的虚拟环境中运行：

```bash
python3 -m meeting_adviser_harness \
  --config ./config.yaml \
  --description-file ./description.md \
  --role-file ./meeting_adviser.md \
  --backend opencode
```

`description.md` 是模拟日程描述，可以同时包含普通文本和直接的飞书 Docx / Wiki 链接。普通文本不访问飞书；出现链接时，使用 `config.yaml` 对应应用在原产品数据目录中已有的用户 OAuth 授权读取文档。读取规则与 2.5 相同：只读取描述中直接引用的 Docx，以及 Wiki 节点指向的 Docx，不递归读取文档内链接。

角色和描述文件必须是本机 UTF-8 普通文件，符号链接会被拒绝。harness 数据默认保存在产品数据目录下的 `harness/`，也可用 `--data-dir` 指定独立目录。

交互终端只显示最终允许回复、显式命令结果和错误；模型保持沉默时不输出，后台状态、材料预览及诊断也不会打断输入。完整 JSON 事件保存在启动时显示的私有 `events/harness.jsonl`，文件有大小上限并轮转。

如需实时查看全部事件，另开一个终端运行：

```bash
python3 -m meeting_adviser_harness watch
```

`watch` 默认读取 `config.yaml` 对应的 `data/harness/events/harness.jsonl`，先显示最近 10 条，再持续跟随新增事件和日志轮转。主命令使用了 `--config` 或 `--data-dir` 时，watch 传入相同参数。

## 模拟发言

每行是一条已经确认的模拟字幕：

```text
张三: 我建议周五发布。
李四：上线前还需要一次回归。
张三: 那就把回归安排在周四。
```

英文冒号和中文冒号都支持。也可以设置当前说话人：

```text
/speaker 张三
第一段发言
第二段发言
/speaker 李四
补充意见
```

每个名字映射到稳定的模拟 speaker ID，每行生成独立 sentence key 和当前时间。前台不会等待模型回答，可以在模型生成期间继续输入；后台会先持久化新字幕，再按正式 consume 的规则让它影响正在等待发送的候选和下一次判断。模型回复不会自动作为新的参会者字幕回灌。

可用命令：

- `/context`：打印最近一次真正提交给模型的 snapshot，包括角色之外的材料、字幕、历史回复、证据键和截断状态。
- `/reload`：停止当前模型 worker，重新读取角色、描述和关联文档，然后在同一模拟会议及字幕历史上继续。
- `/reset`：创建新的模拟会议，不沿用上一轮字幕和回复状态。
- `/quit`：安全停止。

输出中的 `model_decision` 是模型产生并通过结构解析的原始决定；`reply` 且 `status: allowed` 表示该候选又通过了产品的证据、新鲜度、冷却、暂停状态和发送前复核，正式环境此时才会调用飞书发送接口。harness 只打印到终端。

## JSONL 回放

使用 `--script` 可以回放多人对话：

```jsonl
{"speaker":"张三","text":"我建议周五发布。"}
{"speaker":"李四","text":"上线前还需要一次回归。","delay_seconds":1.5}
```

`delay_seconds` 表示提交该行前等待的秒数；省略时立即提交。若要比较真实输入时序，应填写与目标对话相符的间隔。脚本提交完成后，进程等待本轮模型处理结束并以退出码表示是否成功。

## 等价边界

harness 与正式程序共用 `AdviserRunner`、`AdviserStore`、材料解析、上下文预算、模型后端、结构化决定校验、持久化回复队列以及发送前复核。替换的只有三个边界：本地 description 代替日历事件读取，终端文本代替飞书字幕采集，终端打印代替飞书会中消息发送。

因此，在相同角色、材料、字幕内容与时间间隔、模型配置下，它测试的是同一条 LLM 决策和回复处理路径。模型本身可能非确定，不能保证两次输出逐字相同。真实飞书字幕的延迟、修订、说话人识别，以及消息在会议中的最终展示仍需真实会议验证。
