# 2.5 只读上下文探测

首版范围已确认。本脚本用于验证实际日程 / Docx / Wiki 契约与读取身份权限，consume 已接入材料，脚本仍作为独立只读核对入口。默认使用应用身份；2026-10-15 已实现 `--identity user`，授权准备见 [USER_OAUTH.md](USER_OAUTH.md)。无需开始会议或让其他参会者入会；不加入 bot、不发会中消息、不调用 LLM、不修改产品 session 或字幕数据库。

## 准备测试资料

在飞书**日历**创建一条测试日程，添加飞书视频会议。描述放一段便于核对的合成文字，再按正常 UI 操作关联一篇 Docx 和一篇 Wiki 中的 Docx，正文各放不同的合成标记。记录 UI 中关联文档的名称和数量。

需要该日程的 `calendar_id` 和完整 `event_id`。9 位会议号及 `vc.feishu.cn/j/...` 入会链接不包含这两个 ID，不能代替日程绑定。如果会议是直接发起的视频会议，先另建一条日历测试日程。可先提供日程分享链接核对是否包含日程身份；脚本当前不承诺将任意分享链接解析为实例 ID。也可在开放平台 API 调试台按目标日历调用[获取日程列表](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event/list)，核对目标日程的 `event_id`；重复日程必须明确到本次实例，不能只靠相同会议号选择。

沿用本机 mode 600 的 `config.yaml` 中应用凭据，不把凭据放命令行或贴入聊天。日程读取需要相应 API scope 和目标日历 reader 或更高权限；Docx / Wiki 还需要各自读取 scope 与实际资料访问权限。`--identity user` 要求先完成本机 OAuth，不会自动改用用户身份、启动授权或回退到应用身份。

## 独立验证文档可读性

从 `v2/` 执行；替换以下占位符：

```bash
.venv/bin/python -m feishu_meeting_adviser.context_probe \
  --document-url 'https://your-tenant.feishu.cn/wiki/节点token'
```

Wiki 先解析 `obj_type / obj_token`，只有实际类型为 Docx 才读取其元数据和纯文本；其他类型明确标为 unsupported。手工提供的 URL 标记为 `explicit_check_only`，即使成功也不说明该文档已从日程 API 被发现。仅读取指定节点及实际 Docx，不打开正文中其他引用。

## 核对日程关联

```bash
.venv/bin/python -m feishu_meeting_adviser.context_probe \
  --calendar-id '目标calendar_id' \
  --event-id '目标event_id' \
  --meeting-no '九位会议号' \
  --document-url 'https://your-tenant.feishu.cn/docx/文档token' \
  --document-url 'https://your-tenant.feishu.cn/wiki/节点token'
```

`--meeting-no` 可省略；它只用于对照日程返回的明确 `/j/九位会议号` 链接，不执行入会，也不能证明重复日程唯一性。其他会议链接格式报告 unknown，不据此猜测会议对应关系。

输出包含日程描述预览、附件清单、描述中发现的文档数量、Wiki 实际类型、文档标题 / 元数据版本及正文预览。默认每段最多 2000 字符，保留原字符数和 truncated 标记；附件清单最多 30 项，最多读取 10 个不同文档。各类缺失字段、接口失败、空清单与未支持对象明确区分，不转储原始响应或服务端错误正文。

从日程 description 和 description_rich 的 HTML 锚点、Markdown 及直接文本链接识别并去重 Docx / Wiki，记录引用来自哪个字段；附件 `file_token` 原样标为未解析的日历附件，不能当作 Docx ID。若 UI 显示关联文档而 API 无可解析引用，会报告 `not_found_in_event`；人工给出的对照链接不会被补进日程发现结果。此时保留可行性限制，先核对实际返回和官方能力，再决定接入方案。

退出码：0 表示本次请求的读取检查成功，日程未请求时不表示日程验证通过；1 表示读取 / 配置 / 保存失败；2 表示引用未发现、类型未支持、数量超预算或会议链接明确不匹配；130 表示中断。

## 内容预览与清理

默认在本机终端显示内容预览，不保存正文文件。可追加 `--save-report`，将有界报告保存于配置的 `storage.directory/context_probe/`：目录 700、报告 600、每份最多 250 KB、随机文件名不覆盖旧报告。报告包含规范化来源和内容预览，不包含应用凭据、认证头、token 响应或原始 API payload。

这是一次性前置验证报告，尚不绑定产品内部 meeting_id；**当前 `delete-meeting-data` 不删除此目录**。核对后单独删除报告文件；它没有自动轮转和保留任务，不应反复长期留存。终端副本也按会议资料处理。consume 的内容日志已按会议隔离并纳入轮转、保留及统一删除；探测报告仍需单独清理。

## 已验证结果

2026-10-14 应用身份可读取用户提供的 Wiki 中的 Docx（元数据及 88 字正文），但只能列出自身主日历，指定窗口没有目标日程；开通 API scope 不等于拥有用户日历 ACL。

2026-10-15 用户 OAuth 授权成功。只查询选定用户日历的已知时间窗口，返回 2 条日程、1 条会议链接精确匹配。指定日程返回普通 description（36 字符）和 description_rich（93 字符）；可解析的 Wiki 链接仅在 description_rich 中。仅依赖日程返回、没有手工补入文档 URL 的探测，成功发现 1 个引用、解析为 Docx、读取元数据及 88 字正文。上传附件字段未返回，报告标为 field_absent，与显式空列表区分；UI 上传区为空。

合成探测测试覆盖只读端点、单页列表、明确窗口匹配、HTML / Markdown 富文本引用、去重、真实 Wiki 底层 token、未支持类型、附件 token 隔离、失败和空清单、数量预算及私有报告。完整实施验证见 [STAGE2_CALENDAR_VALIDATION](STAGE2_CALENDAR_VALIDATION.md)。真实 ID、来源 token、正文和报告只保存在本机忽略数据目录。
