# Auth 服务契约（OAuth 凭据托管）

状态：v1 于 2026-11-10 冻结（阶段 0.4）。**v1.1（2026-11-11）** 新增 §8：NSDP token 提交通道（组织者个人 token，表单提交、无刷新语义），凭据存储增加 provider 维度。**v1.2（2026-11-12）** 新增 GUI 发现式登录：`/authorize/start` 允许 `intent:"gui:login"` 时 `open_id` 缺省（discovery state，回调后采纳 user_info 主体）；回调成功后 state 记录补存 `user_info` 摘要；新增内部端点 `GET /authorize/status`（advisor 轮询登录进度，见 [macos-gui/DESIGN](../macos-gui/DESIGN.md)）。服务端为 `auth`。语义自旧 `legacy/local-meeting-recorder/src/local_meeting_recorder/oauth.py` 平移（2026-11-10 已对真实飞书 API 形状验证），改为云端多用户（按 open_id 隔离）+ HTTPS 回调。变更需 bump 版本并同步 advisor/auth 两侧实现与测试。上游设计：[ARCHITECTURE](ARCHITECTURE.md) D3。

## 1. 范围、端点与角色

- **auth 只做凭据**：授权 URL 生成、回调换取、静默刷新、按 open_id 保管。不做业务校验（组织者/时间窗在 advisor）；不碰 IM。
- **调用面两类**：
  - 内部 API（HTTP，`127.0.0.1:8381`，仅 advisor，v1 无认证）；
  - OAuth 面（HTTPS，`https://<域名>/oauth/callback`，浏览器经 TLS 反代到达；域名可仅内网/VPN 解析，见 [DEPLOYMENT](DEPLOYMENT.md) §2）。
- 飞书协议端点与参数沿用旧实现已验证常量：

| 用途 | 端点 |
|---|---|
| 授权页 | `https://accounts.feishu.cn/open-apis/authen/v1/authorize`（PKCE S256 + `prompt=consent`） |
| code 换取 | `https://open.feishu.cn/open-apis/authen/v2/oauth/token` |
| 刷新 | `https://accounts.feishu.cn/oauth/v3/token` |
| 身份 | `https://open.feishu.cn/open-apis/authen/v1/user_info` |

- scopes：`calendar:calendar:read`、`calendar:calendar.event:read`、`offline_access`。
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
- **发现式登录变体（v1.2）**：`intent` 为 `gui:login` 时允许 `open_id` 缺省（`null`）——state 记录 `discovery=true`、不绑定 open_id；用于 macos-gui 扫码登录（身份即待发现项）。回调时以 user_info 的 open_id **采纳**为主体（跳过防串换相等校验，见 §4.1）；非 discovery state 不受影响。

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
  "en_name": "Kai Han", "email": "kai.han@nio.com"}}
```

- `pending`：state 存在、未过期、回调尚未完成；`completed`：回调成功、凭据已按 open_id 落库（幂等可重复读）。不存在或 TTL 过期 → `404 {"code": "not_found"}`。
- 仅返回 §4.1 落存的 user_info 摘要字段；**任何情况下不返回 token**（access/refresh 一律不出本服务，红线同 §1）。
- 供 advisor 的 `/gui/login/poll` 轮询（CONTRACT_ADVISOR_API §8.2）；非 discovery state 查询此端点同样适用（`completed` 不带 `user` 字段，除非该 state 落存过摘要）。

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
4. 落盘成功后返回简化成功页：显示"授权成功，请回到飞书重新发送刚才的指令（<intent 摘要>）"；discovery state 显示"登录成功，请回到采集应用"。
5. 任何一步失败：显式失败页（含原因），不静默重试；code 一次性，失败不重放。
6. **user_info 摘要落 state 记录（v1.2）**：成功路径将 `{open_id, name, en_name, email}` 写入 state 记录（供 `GET /authorize/status` 读取；discovery 与非 discovery 均落）。摘要不含任何 token；state 表 TTL 回收语义不变。

### 4.2 `GET /oauth/callback?error=…`（用户拒绝授权）

释放对应 state 记录，返回"已取消授权"页。已有凭据不受影响。

## 5. 存储格式与权限

- 目录 `<auth 数据目录>/credentials/`（0700），每 open_id 一文件（0600），≤64 KiB。
- 原子写：临时文件 + fsync + `rename` + 目录 fsync；读取 `O_NOFOLLOW`（照搬旧实现）。
- 记录字段：`{version:1, open_id, state: ready|refreshing|reauthorization_required, access_token, refresh_token, expires_at, refresh_expires_at, scopes, refresh_margin}`。
- state 记录（PKCE/state 表）独立于凭据文件，同样原子写、带 TTL 回收。

## 6. 时序说明（验收口径）

- **过期（静默刷新）**：token 临期 → advisor 调 `/token` → auth 单飞刷新（marker→POST→轮转替换）→ 返回新 token，小A 无感。
- **拒绝**：小A 在授权页点"取消" → 飞书回调 `error=` → auth 释放 state、返回取消页；bot 未收到任何凭据。
- **重放**：同一 `state`/`code` 二次到达 → state 已用，显式失败页；同 code 不可再次换取。
- **并发刷新**：advisor 两个线程同时请求同一 open_id 的临期 token → 仅一次 POST，两个调用同得新 token；期间崩溃 → 记录停留 `refreshing` → 后续请求 `reauthorization_required`，绝不重放。

## 7. 版本

API v1（v1.1 见 §8；v1.2 变更：§2.1 discovery 变体、§2.3 `/authorize/status`、§4.1 采纳语义与 user_info 摘要落存）。不兼容变更递增版本号，并同步 auth/advisor 两侧实现、测试与 `integrations/` 用例。

## 8. NSDP token 提交通道（v1.1）

组织者（小A）的个人 NSDP token：长期有效、无过期时间可判、无刷新语义。失效只能在使用时由 401 发现（advisor 侧），失效即删除凭据（下次读取 `not_authorized` → 重新走提交流程）。仅组织者需要；小B 无此通道。

### 8.1 内部 API（仅 advisor 调用）

- `POST /token-submissions/start` `{"open_id": "ou_A", "provider": "nsdp"}` → 200 `{"submit_url": "https://<域名>/nsdp/submit?state=…", "state": "rnd-…", "expires_at": …}`。state 复用 §2.1 的 state 表语义（随机、一次性、TTL 10 min、绑定 open_id；intent=`nsdp:token` 与飞书 OAuth state 区分）。
- `GET /credentials/{open_id}/token?provider=nsdp` → 200 `{"token": "…", "updated_at": …}`；无记录 → 404 `not_authorized`。
- `POST /credentials/{open_id}/invalidate` `{"provider": "nsdp"}` → 200 `{"ok": true}`（幂等；advisor 观察到 NSDP 401 后调用）。

### 8.2 HTTPS 面（浏览器表单，与 OAuth 回调同面同约束）

- `GET /nsdp/submit?state=…`：校验 state（存在、未用、未过期、intent 匹配；**不核销**，刷新页面可用）→ 极简表单页（`type=password`、`autocomplete=off`、无第三方资源）。
- `POST /nsdp/submit?state=…`（`application/x-www-form-urlencoded`，单字段 `token`）：**核销 state（一次性）** → 格式校验（可打印、8..512 字符；**不验活**——保持 auth 只做凭据不出网，错误 token 由使用时 401 暴露）→ 按 open_id 存储 → 成功页（"回到飞书重发刚才的指令"，不回显 token）。任何失败 → 显式失败页，不重放。
- 重复提交同一 state → 400 失败页；已保存的旧 token 不被覆盖。

### 8.3 存储与红线

- `<auth 数据目录>/credentials/nsdp/<open_id>.json`（0700/0600、原子写、`O_NOFOLLOW`，同 §5 纪律）：`{version:1, provider:"nsdp", open_id, token, updated_at}`。
- 红线：NSDP token 绝不进入 IM 文本（链接里只有 state）、不下发 laptop、不出现在任何 HTML 页面（含错误页）；仅 advisor 经内部 API 读取，用后不留存；提交时不验活、不回显。
