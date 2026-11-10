# Auth 服务契约（OAuth 凭据托管）

状态：v1 于 2026-11-10 冻结（阶段 0.4）。服务端为 `new/auth`。语义自旧 `local-meeting-recorder/src/local_meeting_recorder/oauth.py` 平移（2026-11-10 已对真实飞书 API 形状验证），改为云端多用户（按 open_id 隔离）+ HTTPS 回调。变更需 bump 版本并同步 advisor/auth 两侧实现与测试。上游设计：[ARCHITECTURE](ARCHITECTURE.md) D3。

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
- **红线**：user_access_token/refresh_token 绝不进入 IM 文本、不下发 laptop；内部 API 响应仅给 advisor，用后不留存。

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
3. 经 user_info 取得授权者 open_id，**必须等于 state 绑定的 open_id**（防串换账号），否则显式失败页。
4. 落盘成功后返回简化成功页：显示"授权成功，请回到飞书重新发送刚才的指令（<intent 摘要>）"。
5. 任何一步失败：显式失败页（含原因），不静默重试；code 一次性，失败不重放。

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

API v1。不兼容变更递增版本号，并同步 auth/advisor 两侧实现、测试与 `new/integrations/` 用例。
