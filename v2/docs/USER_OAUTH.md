# 日程材料的用户 OAuth

2026-10-15 用户已确认并完成用户 OAuth，用当前用户身份读取指定日历日程及其 Docx / Wiki 材料；consume 已接入启动时材料快照。VC 入会、字幕和会中发送继续沿用应用 token。

## 飞书应用后台准备

沿用本机 `config.yaml` 中的应用凭据，无需复制 App Secret 或 token 到命令行、浏览器或聊天。

1. 在应用 **安全设置 → 重定向 URL** 添加这个完整地址：

   ```text
   http://127.0.0.1:8765/oauth/callback
   ```

2. 在 **权限管理** 确认用户身份所需权限已开通：

   | Scope | 用途 |
   | --- | --- |
   | `calendar:calendar:read` | 列出日历元数据以取得明确绑定 ID |
   | `calendar:calendar.event:read` | 读取指定日程及限定时间窗口 |
   | `docx:document:readonly` | 读取指定 Docx 元数据与纯文本 |
   | `wiki:node:read` | 解析指定 Wiki 节点的实际类型 / 对象 |
   | `offline_access` | 返回 refresh token，供持续运行时刷新 |

3. 如后台显示刷新 `user_access_token` 开关，确认开启；没有此开关时无需寻找，官方默认开启。发布应用使配置生效，授权账号必须具备该应用的使用权限。

不会请求日历编辑、共享人修改、文档写入或通讯录联系方式权限。OAuth grant 的读取 scope 本身不限定到一条日程，产品的材料访问仍按用户选定的日历 / 日程和明确关联来源限制，不能据此遍历所有日历正文。授权页显示的范围应与上表核对。

## 在本机授权

从 `v2/` 执行：

```bash
.venv/bin/python -m feishu_meeting_adviser.oauth login
```

程序只监听 `127.0.0.1:8765`，打印本机入口 `http://127.0.0.1:8765/`。在**运行脚本的同一台电脑**用浏览器打开入口，选择目标日历所属账号并在飞书授权页确认。回调成功后会显示凭据已保存在本机，命令返回安全状态；授权码和 token 不需手工复制。默认等待 10 分钟，可用 `--timeout 1800` 等待 30 分钟；Ctrl-C 可停止等待。

使用随机 state、S256 PKCE 和显式 consent 页面；本机回调校验 Host、路径、state 和参数唯一性，拒绝错误 / 重复参数。回调 HTTP 请求和提供商错误正文不打印，页面不回显授权码或凭据。实际用户身份只保存为应用内用户标识的 hash，不保存用户信息接口返回的姓名、邮箱或电话。

若端口被占用，可停止旧授权进程或显式指定 `login --port 8766`；同时必须在飞书后台添加完全一致的 `http://127.0.0.1:8766/oauth/callback`。不要将监听地址改为公网地址。

检查与清理：

```bash
.venv/bin/python -m feishu_meeting_adviser.oauth status
.venv/bin/python -m feishu_meeting_adviser.oauth logout
```

等待授权时持有独立 OAuth 锁，此时另一个用户身份读取 / status / logout 会明确报 busy；先完成或停止 login。logout 只删除本机凭据，飞书侧的授权在飞书授权管理中撤销；不要将本机删除理解为远端撤销。

## 授权后定位测试日程

先只列一页日历元数据，选中目标日历 ID，不读取日程正文：

```bash
.venv/bin/python -m feishu_meeting_adviser.context_probe \
  --identity user --list-calendars
```

有更多页时输出 `has_more` 与 `page_token`，下一页由用户明确传入 `--page-token '返回的分页标记'`，不自动扫描所有日历。

对已选日历，在明确时间窗口内对照已知会议号：

```bash
.venv/bin/python -m feishu_meeting_adviser.context_probe \
  --identity user --find-event \
  --calendar-id '目标calendar_id' \
  --start '2026-10-14T15:30:00+08:00' \
  --end '2026-10-14T19:30:00+08:00' \
  --meeting-no '九位会议号'
```

这里的时间仅演示已提供截图的窗口，应替换为目标日程的实际时间；窗口必须包含时区、跨度不超过 24 小时。只保留视频会议链接明确匹配的候选，多条候选或上游截断均不能直接认定唯一绑定。探测不自动保存绑定、切换会议或触发回答。

取得并核对完整 `event_id` 后，按[上下文探测说明](CONTEXT_PROBE.md)传入指定日程及文档对照链接，并加 `--identity user`。报告正确标记用户身份，失败不会回退为应用读取结果。

## 凭据和恢复边界

用户凭据独立保存于私有 `data/oauth/<应用标识hash>.json`（数据目录可配置），目录 700、文件 600、写入原子替换并 fsync。每个应用使用一个本机用户授权，以用户 subject hash 区分重新授权账号。此文件只保存当前 access / refresh token、scope、有效期及状态，不保存授权码、PKCE verifier 或原始 provider 响应。

按服务端有效期提前刷新，同一应用凭据的刷新用独立锁串行执行。refresh token 为一次性凭据：请求前先持久化 refreshing 状态，收到结果后原子保存新的 token；请求结果不确定、返回非法结果或 POST 后本机中断均要求重新授权，禁止重放旧 refresh token。明确的用户 access token 失效最多刷新并重试读取一次，scope / 资料 ACL 错误不靠刷新重试，不获取 tenant token。

凭据是跨会议共享的认证状态，`delete-meeting-data` 不删除 OAuth 文件；停止使用授权时单独执行 logout，必要时在飞书侧撤销。会议资料和内容预览报告的删除仍按各自操作说明执行。私有权限和删除不承诺存储介质物理擦除。

## 接口版本与实施验证

授权入口为 `accounts.feishu.cn/open-apis/authen/v1/authorize`，S256 PKCE 授权码使用 `open.feishu.cn/open-apis/authen/v2/oauth/token` 的 JSON 请求交换；刷新采用 `accounts.feishu.cn/oauth/v3/token` 的 form 请求。v3 刷新接口支持 v2 签发的 token。此前将该授权入口的 PKCE 码交给 v3 交换返回 20049，已修正组合；失败不尝试另一端点或重用已消费授权码，需重新从本机入口授权。

12 项 OAuth 测试覆盖 PKCE / state、精确交换与刷新请求、私有保存、刷新串行与轮换、请求前持久化标记、不确定结果禁止重放、scope、损坏 / 跨应用凭据、真实本机 HTTP 回调及身份隔离。日历查找和描述富文本解析另有合成测试；累计结果见 [2.5 验证记录](STAGE2_CALENDAR_VALIDATION.md)。

真实授权成功后，只对选定用户日历查询已提供的测试时间窗口，返回 2 条日程，会议链接精确匹配 1 条。指定日程的 description_rich 返回一个 Wiki 引用，解析为 Docx 后成功读取元数据和 88 字正文。未将真实日程 ID、来源 token 或正文提交仓库。OAuth 成功不意味着其他日历和资料均可访问，运行仍须显式绑定并满足 ACL。

## 官方契约

- [授权码与回调](https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code)：scope、重定向白名单、state、PKCE 与 consent。
- [获取 user_access_token（v2）](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token)：授权码交换、JSON 请求和 PKCE verifier。
- [刷新 user_access_token（v3）](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/refresh-user-access-token-v3)：offline_access、刷新开关和一次性 refresh token。
- [获取用户信息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get)：应用内 open_id；不额外申请联系方式字段权限。
