# Auth 服务契约（OAuth 凭据托管）

> **v1.8（2026-11-28，admin-site 登录）**：`/authorize/start` 的发现式登录扩展 `intent:"admin:login"`（`open_id` 必须缺省）。回调成功后不再渲染页面，而是 302 到 admin-site 回调（默认 `<base_url>/admin/oauth/callback`，可用 `auth serve --admin-callback-url` 覆盖；本地开发为 `http://127.0.0.1:8390/oauth/callback`），由 admin-site 校验自身 state cookie、调用 `/authorize/status` 读取身份并按 `ADMIN_USERS` 白名单决定是否建会话。其他 intent（`gui:login`、`join:*`、`prompt-studio`、集成床 `bed`）行为完全不变；回归用例见 `auth/tests/test_api.py`。additive，存量消费方不受影响。
>
> **v1.7（2026-11-17，supervisor login 解析）**：身份摘要（§2.3 status、§2.4 profile、§4.1 落存）新增 `user_id` 字段——飞书 user_info 返回的租户内登录名（如 `kai.han`），可为 `null`（以实际返回为准）。存量档案缺 `user_id` 时，§2.4 的惰性补档同样适用（下次凭据读取时 best-effort 重取一次）。供 advisor supervisor grant 按 login 解析（CONTRACT_ADVISOR_API §9.4）。additive，存量消费方不受影响。
>
> **v1.6（2026-11-16，SM-01/SM-08）**：用户 OAuth scope 集扩展——新增用户版 `vc:meeting.meetingevent:read`（会话观测轮询，见 studies/meeting-events `poll_user.py` 链路）。**请求集合**（授权 URL/code 换取/刷新均携带）= BASE ∪ {`vc:meeting.meetingevent:read`}；**校验下限** BASE = 原 3 项（§1）。存量凭据（无 vc scope）继续通过校验与刷新，不强制重授权；缺该 scope 的 token 用于会话观测时由使用方（advisor）按「观测能力缺失 → unknown + 一次性提醒重新授权」处理。stub transport（`integrations/`）同步跟随请求集合。
>
> **v1.5（2026-11-14）Jira PAT 扩展**：支持组织者 Jira PAT 的一次性表单提交及受控只读查询，见 §9。
> 入会时 advisor 分别检查三种 provider 的凭据是否存在或可用；NSDP/Jira 提交不验活，详见 CONTRACT_ADVISOR_API v1.6。
>
> **v1.4（2026-11-14）NSDP 工具凭据边界扩展**：可信 llm-gateway 的受控查询工具可按 advisor 提交并绑定的会话组织者身份调用 §8 的 NSDP token 读取接口。token 仅进入查询子进程的环境变量，不进入 OpenCode 进程、模型输入、推理任务存储或日志。工具按需读取，执行结束即释放；gateway 和 auth 的内部接口仍仅限可信网络。组织者身份传递见 [推理身份扩展](CONTRACT_INFERENCE_IDENTITY.md)。查询遇到缺失或 401 时工具返回结构化授权错误，advisor 负责通知组织者；当前不自动删除凭据，避免并发重新授权时误删新 token。飞书凭据的调用边界不变。

状态：v1 于 2026-11-10 冻结（阶段 0.4）。**v1.1（2026-11-11）** 新增 §8：NSDP token 提交通道（组织者个人 token，表单提交、无刷新语义），凭据存储增加 provider 维度。**v1.2（2026-11-12）** 新增 GUI 发现式登录：`/authorize/start` 允许 `intent:"gui:login"` 时 `open_id` 缺省（discovery state，回调后采纳 user_info 主体）；回调成功后 state 记录补存 `user_info` 摘要；新增内部端点 `GET /authorize/status`（advisor 轮询登录进度，见 [gui/DESIGN](../gui/DESIGN.md)）。**v1.3（2026-11-13）** 新增身份档案：回调持久化 user_info 摘要至 `<数据目录>/profiles/`，新端点 `GET /users/{open_id}/profile`（§2.4，供 advisor 身份目录/运维台显示实名）。服务端为 `auth`。语义自旧方案 B `oauth.py` 平移（2026-11-10 已对真实飞书 API 形状验证；源文件已随 `legacy/` 删除，存 git 历史），改为云端多用户（按 open_id 隔离）+ HTTPS 回调。变更需 bump 版本并同步 advisor/auth 两侧实现与测试。上游设计：[ARCHITECTURE](ARCHITECTURE.md) D3。

## 1. 范围、端点与角色

- **auth 只做凭据**：授权 URL 生成、回调换取、静默刷新、按 open_id 保管。不做业务校验（组织者/时间窗在 advisor）；不碰 IM。
- **调用面两类**：
  - 内部 API（HTTP，`127.0.0.1:8381`，advisor 与受控 NSDP 查询工具，v1 无认证）；
  - OAuth 面（HTTPS，`https://<域名>/oauth/callback`，浏览器经 TLS 反代到达；域名可仅内网/VPN 解析，见 [DEPLOYMENT](DEPLOYMENT.md) §2）。
- 飞书协议端点与参数沿用旧实现已验证常量：

| 用途 | 端点 |
|---|---|
| 授权页 | `https://accounts.feishu.cn/open-apis/authen/v1/authorize`（PKCE S256 + `prompt=consent`） |
| code 换取 | `https://open.feishu.cn/open-apis/authen/v2/oauth/token` |
| 刷新 | `https://accounts.feishu.cn/oauth/v3/token` |
| 身份 | `https://open.feishu.cn/open-apis/authen/v1/user_info` |

- scopes（v1.7）：**BASE**（校验下限，存量与刷新必须满足）= `calendar:calendar:read`、`calendar:calendar.event:read`、`offline_access`；**请求集合**（授权/换取/刷新时实际申请）= BASE + `vc:meeting.meetingevent:read`（用户版，用于 advisor 会话观测轮询）+ `docx:document:readonly` + `wiki:wiki:readonly`（用于建会时读取会议描述中的飞书文档作为背景材料）。凭据记录校验（换取/刷新/读取）只要求 ⊇ BASE，多出的 scope 允许；因此新增 scope 不使存量凭据失效。**存量凭据没有新增的文档 scope**：advisor 侧检测到缺失时降级为只带会议描述建会，并给组织者发一次重新授权提示（1 小时去重），不阻塞入会。
- redirect URI 必须与飞书应用后台注册值**精确匹配**（`https://<域名>/oauth/callback`）。
- **红线**：user_access_token/refresh_token 绝不进入 IM 文本、不下发 laptop；内部 API 响应仅给 advisor，用后不留存。**v1.1 起红线同样覆盖 NSDP token（§8）。**
- **v1.1**：`GET /credentials/{open_id}/token` 接受 `?provider=feishu|nsdp`（缺省 feishu，向后兼容）；飞书响应 `{access_token, expires_at, scopes}`，NSDP 响应 `{token, updated_at}`。

## 2. 内部 API（仅 advisor 调用）

### 2.1 `POST /authorize/start`

```json
{"open_id": "ou_A", "intent": "join:123456789"}
```

→ 200：

```json
{"authorize_url": "https://accounts.feishu.cn/…/authorize?…",
 "state": "rnd-…", "expires_at": "2026-11-10T14:10:00+08:00"}
```

- 生成 PKCE `verifier`/`challenge` 与 `state`；`state` 随机不可猜、**一次性**、TTL 10 min，服务端记录 `(state → open_id, intent, verifier, used)`。
- `intent` 为待续接意图（如 `join:<会议号>`），仅用于回调成功页提示，不含敏感信息。
- advisor 将 `authorize_url` 经 CONTRACT_ADVISOR_API 的 `authorization_required` 响应转给 bot → 小A。
- **发现式登录变体（v1.2）**：`intent` 为 `gui:login` 时允许 `open_id` 缺省（`null`）——state 记录 `discovery=true`、不绑定 open_id；用于 gui 扫码登录（身份即待发现项）。回调时以 user_info 的 open_id **采纳**为主体（跳过防串换相等校验，见 §4.1）；非 discovery state 不受影响。
- **admin-site 登录变体（v1.8）**：`intent` 为 `admin:login` 时同样允许 `open_id` 缺省（`null`），语义同 discovery；回调成功后 302 到 admin-site 回调（默认 `<base_url>/admin/oauth/callback?state=…`，`--admin-callback-url` 可覆盖，见 §4.1）。

### 2.2 `GET /credentials/{open_id}/token[?rejected=<token>]`

→ 200：

```json
{"access_token": "u-xxx", "expires_at": 734819400, "scopes": ["calendar:calendar:read", …]}
```

语义（照搬旧 `access_token()`，含并发单飞）：

1. 无该 open_id 记录 → `404 {"code":"not_authorized"}`。
2. 记录 `state != ready`（含刷新中途崩溃残留 `refreshing`）→ `409 {"code":"reauthorization_required"}`（fail-closed，一次性凭据绝不重放）。
3. 有效即返：`now < expires_at − refresh_margin` 且未传 `rejected`（或 `rejected` 与当前 token 不一致）→ 直接返回。
4. 临期/被拒 → **单飞刷新**（第 3 节）；同 open_id 并发请求只执行一次 POST，其余等待同一结果。
5. 刷新路径上的任何失败（网络不可达、响应非法、refresh 未轮转）或 `refresh_expires_at` 已过 → 凭据已按 fail-closed 置 `reauthorization_required`，返回 `409`。刷新 POST 一旦发出，一次性 refresh token 可能已被消费，客户端不得原样重试（2026-11-10 实现期修正：原文"503 不改状态"与 marker 先行语义冲突）。
6. `refresh_margin = min(300, expires_in / 2)`。

### 2.3 `GET /authorize/status?state=rnd-…`（v1.2，仅 advisor 调用）

→ 200：

```json
{"status": "pending"}
```

或（discovery state 完成后）：

```json
{"status": "completed", "user": {"open_id": "ou_A", "name": "Kai Han",
  "en_name": "Kai Han", "email": "kai.han@nio.com", "user_id": "kai.han"}}
```

- `pending`：state 存在、未过期、回调尚未完成；`completed`：回调成功、凭据已按 open_id 落库（幂等可重复读）。不存在或 TTL 过期 → `404 {"code": "not_found"}`。
- 仅返回 §4.1 落存的 user_info 摘要字段；**任何情况下不返回 token**（access/refresh 一律不出本服务，红线同 §1）。
- 供 advisor 的 `/gui/login/poll` 轮询（CONTRACT_ADVISOR_API §8.2）；非 discovery state 查询此端点同样适用（`completed` 不带 `user` 字段，除非该 state 落存过摘要）。

### 2.4 `GET /users/{open_id}/profile`（v1.3，仅 advisor 调用）

→ 200：

```json
{"open_id": "ou_A", "name": "Kai Han", "en_name": "Kai Han",
 "email": "kai.han@nio.com", "user_id": "kai.han", "updated_at": 1762900000.0}
```

- 返回 §4.1 第 7 步持久化的身份档案（name/en_name/email/user_id 可为 `null`，以飞书 user_info 实际返回为准）。
- **存量用户惰性补档**：v1.3 之前已授权、尚无档案的用户，在下一次 `GET /credentials/{open_id}/token` 命中可用 token 时由 auth 顺带 best-effort 补档一次（失败不影响 token 返回，缺档期间重复尝试）。**v1.7 起已存档但缺 `user_id` 的档案同样按此语义惰性重取**。
- 用户从未授权 → `404 {"code": "unknown_user"}`；open_id 非法 → 400 `bad_message`。
- **任何情况下不返回 token**（红线同 §1）；供 advisor 身份目录（CONTRACT_ADVISOR_API §9.1）拉取显示名。

## 3. 刷新语义（marker 先行、必须轮转）

1. 刷新前**先原子提交 marker**：记录置 `state=refreshing`（旧实现"Commit before the one-use POST"语义）。
2. 执行一次性 POST（`grant_type=refresh_token`）。
3. 响应中 **refresh_token 必须轮转**：新值 == 旧值 → 视为非法响应，记 `reauthorization_required`。
4. 成功 → 以新记录原子替换（access/refresh/expires/refresh_expires/scopes 全量替换）。
5. 任何失败（网络/协议/校验）→ 记 `reauthorization_required` 并向调用方返回该错。
6. **崩溃规则**：marker 提交后任意时刻崩溃，记录停留 `refreshing`，下次读取即 `reauthorization_required`；绝不重放可能已被消费的一次性 refresh token。

## 4. OAuth 面（浏览器）

### 4.1 `GET /oauth/callback?code=…&state=…`

1. `state` 校验：存在、未过期、**未使用** → 立即标记已用（一次性）。
2. 用 state 记录中的 `verifier` 完成 PKCE code 换取。
3. 经 user_info 取得授权者 open_id：**非 discovery state** 必须等于 state 绑定的 open_id（防串换账号），否则显式失败页；**discovery state（v1.2，`intent:"gui:login"`）** 无绑定可校验，以 user_info 的 open_id **采纳**为主体（发现式登录语义）。
4. 落盘成功后返回简化成功页：显示"授权成功，请回到飞书重新发送刚才的指令（<intent 摘要>）"；discovery state 显示"登录成功，请回到采集应用"。**例外（v1.8）**：`intent:"admin:login"` 不渲染页面，改为 302 `Location: <base_url>/admin/oauth/callback?state=…`（`Cache-Control: no-store`、`Referrer-Policy: no-referrer`），其余 intent 行为不变。
5. 任何一步失败：显式失败页（含原因），不静默重试；code 一次性，失败不重放。
6. **user_info 摘要落 state 记录（v1.2）**：成功路径将 `{open_id, name, en_name, email, user_id}` 写入 state 记录（供 `GET /authorize/status` 读取；discovery 与非 discovery 均落）。摘要不含任何 token；state 表 TTL 回收语义不变。
7. **身份档案持久化（v1.3）**：成功路径同时将同一摘要写入持久档案 `<数据目录>/profiles/<open_id>.json`（§5），供 `GET /users/{open_id}/profile`（§2.4）读取；写失败按第 5 条显式失败处理，不产生半凭据状态。

### 4.2 `GET /oauth/callback?error=…`（用户拒绝授权）

释放对应 state 记录，返回"已取消授权"页。已有凭据不受影响。

## 5. 存储格式与权限

- 目录 `<auth 数据目录>/credentials/`（0700），每 open_id 一文件（0600），≤64 KiB。
- 身份档案目录 `<auth 数据目录>/profiles/`（v1.3，0700），每 open_id 一文件（0600）：`{version:1, open_id, name, en_name, email, user_id, updated_at}`，不含任何 token；与凭据文件同样的原子写/`O_NOFOLLOW` 语义。
- 原子写：临时文件 + fsync + `rename` + 目录 fsync；读取 `O_NOFOLLOW`（照搬旧实现）。
- 记录字段：`{version:1, open_id, state: ready|refreshing|reauthorization_required, access_token, refresh_token, expires_at, refresh_expires_at, scopes, refresh_margin}`。
- state 记录（PKCE/state 表）独立于凭据文件，同样原子写、带 TTL 回收。

## 6. 时序说明（验收口径）

- **过期（静默刷新）**：token 临期 → advisor 调 `/token` → auth 单飞刷新（marker→POST→轮转替换）→ 返回新 token，小A 无感。
- **拒绝**：小A 在授权页点"取消" → 飞书回调 `error=` → auth 释放 state、返回取消页；bot 未收到任何凭据。
- **重放**：同一 `state`/`code` 二次到达 → state 已用，显式失败页；同 code 不可再次换取。
- **并发刷新**：advisor 两个线程同时请求同一 open_id 的临期 token → 仅一次 POST，两个调用同得新 token；期间崩溃 → 记录停留 `refreshing` → 后续请求 `reauthorization_required`，绝不重放。

## 7. 版本

API v1（v1.1 见 §8；v1.2 变更：§2.1 discovery 变体、§2.3 `/authorize/status`、§4.1 采纳语义与 user_info 摘要落存；v1.3 变更：§2.4 `GET /users/{open_id}/profile`、§4.1 第 7 步档案持久化、§5 profiles 目录；v1.4/v1.5 见文首变更记录；v1.6 变更：§1 scopes 拆分 BASE/请求集合并新增用户版 `vc:meeting.meetingevent:read`，存量凭据不受影响；**v1.7 变更：身份摘要新增 `user_id` 字段（§2.3/§2.4/§4.1/§5），存量档案缺 `user_id` 时惰性补档**）。不兼容变更递增版本号，并同步 auth/advisor 两侧实现、测试与 `integrations/` 用例。

## 8. NSDP token 提交通道（v1.1）

组织者（小A）的个人 NSDP token：长期有效、无过期时间可判、无刷新语义。失效只能在工具使用时由 401 发现；v1.4 不自动删除，通知组织者重新提交以覆盖旧 token。仅组织者需要；小B 无此通道。

### 8.1 内部 API（advisor 与受控 NSDP 查询工具）

- `POST /token-submissions/start` `{"open_id": "ou_A", "provider": "nsdp"}` → 200 `{"submit_url": "https://<域名>/nsdp/submit?state=…", "state": "rnd-…", "expires_at": …}`。state 复用 §2.1 的 state 表语义（随机、一次性、TTL 10 min、绑定 open_id；intent=`nsdp:token` 与飞书 OAuth state 区分）。
- `GET /credentials/{open_id}/token?provider=nsdp` → 200 `{"token": "…", "updated_at": …}`；无记录 → 404 `not_authorized`。
- `POST /credentials/{open_id}/invalidate` `{"provider": "nsdp"}` → 200 `{"ok": true}`（幂等；advisor 观察到 NSDP 401 后调用）。

### 8.2 HTTPS 面（浏览器表单，与 OAuth 回调同面同约束）

- `GET /nsdp/submit?state=…`：校验 state（存在、未用、未过期、intent 匹配；**不核销**，刷新页面可用）→ 极简表单页（`type=password`、`autocomplete=off`、无第三方资源）。
- `POST /nsdp/submit?state=…`（`application/x-www-form-urlencoded`，单字段 `token`）：**核销 state（一次性）** → 格式校验（可打印、8..512 字符；**不验活**——保持 auth 只做凭据不出网，错误 token 由使用时 401 暴露）→ 按 open_id 存储 → 成功页（"回到飞书重发刚才的指令"，不回显 token）。任何失败 → 显式失败页，不重放。
- 重复提交同一 state → 400 失败页；已保存的旧 token 不被覆盖。

### 8.3 存储与红线

- `<auth 数据目录>/credentials/nsdp/<open_id>.json`（0700/0600、原子写、`O_NOFOLLOW`，同 §5 纪律）：`{version:1, provider:"nsdp", open_id, token, updated_at}`。
- 红线：NSDP token 绝不进入 IM 文本（链接里只有 state）、不下发 laptop、不出现在任何 HTML 页面（含错误页）；由受控查询工具经内部 API 按需读取，用后不留存；提交时不验活、不回显。

## 9. Jira PAT 扩展（v1.5，2026-11-14）

`POST /token-submissions/start` 支持 `provider: "jira"`，返回绑定组织者的一次性 `/jira/submit?state=…` HTTPS 表单链接（10 分钟有效）。GET 验证但不核销 state；POST 核销 state 并保存表单 `token`，校验同 NSDP（8..512 个可打印非空白 ASCII 字符），不验活、不回显。Jira 与 NSDP 的 state intent、凭据记录分别隔离；凭据保存在 `credentials/jira/<open_id>.json`，权限及原子写纪律同 §8。

内部接口 `GET /credentials/{open_id}/token?provider=jira` 返回 `{token,updated_at}` 或 404；`POST /credentials/{open_id}/invalidate` 接受 `{"provider":"jira"}`。工具查询缺失或 Jira 返回 401 时通知组织者重新提交，重新提交覆盖旧 PAT；403 视为访问受限，不清理凭据。按需授权，不阻塞会议。生产部署须通过 TLS 反代暴露 `/jira/submit`，不得暴露内部凭据 API。
