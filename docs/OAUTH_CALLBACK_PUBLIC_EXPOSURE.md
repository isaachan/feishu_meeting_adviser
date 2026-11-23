# OAuth 回调公网暴露 · 部署结构分析（2026-11-19）

状态：分析结论 / 变更提案（未实施）。实施时按 §6 任务拆分逐项 commit。
背景：gui 扫码登录要求手机（可能在内网/VPN 之外）扫码后能回访 OAuth 回调，需将 `http://meeting-advisor.nioint.com/oauth/callback` 公网化，域名换为 `meeting-advisor.nio.com`；**仅回调公网可达，其余入口维持内网**。
上游：[DEPLOYMENT](DEPLOYMENT.md) §2/§7、[CONTRACT_AUTH](CONTRACT_AUTH.md) §1、[gui/DESIGN](../gui/DESIGN.md) §4。

## 1. 现状梳理（2026-11-16 生产基线）

```
公网/手机 ──✗──  （v1 基线：后端不接公网）
内网 DNS: meeting-advisor.nioint.com ──► 10.112.238.232（Ubuntu 单机）
  nginx :80（HTTP，无 TLS）
    ├─ /oauth/callback /nsdp/submit /jira/submit ──► auth      127.0.0.1:8381
    ├─ /gui/  ──► advisor    127.0.0.1:8380
    └─ /admin/──► admin-site 127.0.0.1:8390
  ingest ws :8400 直连（0.0.0.0，明文，受信内网）
  其余全部 localhost
```

配置事实：

- `PUBLIC_BASE_URL`（`scripts/prod.env` → auth `--base-url`，`scripts/prod-run.sh:14`）一鱼三吃：
  1. nginx `server_name`（`scripts/prod-up.sh:11` 解析，且**硬编码断言 scheme == "http"**）；
  2. OAuth `redirect_uri`（`auth/src/auth/service.py:37`）；
  3. `/nsdp/submit`、`/jira/submit` 链接（service.py:229/270）。
- GUI 二维码内容 = 飞书授权页 URL 原样透传（advisor/api.py:669 `login_url` = auth `authorize_url`）。手机扫码后**唯一需要回访的只有 `/oauth/callback`**——回调返回静态成功/失败页，无二次跳转；换取、落库均在服务端完成。
- 因此「只公网开 callback」在流程上成立。

## 2. 结论

- **部署形态不变**：单机 systemd + nginx 反代保留，不拆机、不加网关。
- **入口层必须改**：公网 DNS/映射、nginx 双 server block + TLS、auth 拆分 base-url（小代码改动）、飞书后台回调注册、四处文档。
- **一个关键架构事实**：`redirect_uri` 全局唯一——auth 对所有授权流程（GUI 扫码 `gui:login`、bot IM 下发的入会/list/observe 授权链接）用同一个 redirect_uri。注册成公网域名后，**内网桌面浏览器完成授权后也会被跳转到公网域名**，内网必须能解析并到达它（见 §5 风险 1）。

## 3. 推荐方案：双域名（公网入口只放一条路径）

```
meeting-advisor.nio.com    （公网 DNS，443 TLS）
    └─ location = /oauth/callback ──► auth 127.0.0.1:8381   ← 唯一公网路径，其余 404
meeting-advisor.nioint.com （内网 DNS，80，现状不动）
    ├─ /oauth/callback /nsdp/submit /jira/submit ──► auth   8381（回调解耦后仅供内网浏览器）
    ├─ /gui/  ──► advisor    8380
    └─ /admin/──► admin-site 8390
ingest ws :8400、云内 localhost 互调：全部不动
```

- 新 redirect URI 定为 **`https://meeting-advisor.nio.com/oauth/callback`**（公网入口上 TLS；PKCE 已兜底 code 拦截，但不应裸奔明文）。
- submit 表单、GUI、admin、ingest 继续走内网域名，暴露面与现状完全一致。

## 4. 改动清单

### 4.1 网络/DNS（IT 侧前置条件，代码解决不了）

- `meeting-advisor.nio.com` 公网解析 → 公网 IP:443(:80) → 映射到 10.112.238.232（NAT 端口转发或 DMZ 反代）。
- 确认 nio.com 主域备案覆盖子域、公司防火墙放行策略。
- **内网可达性**：内网 DNS 对 `meeting-advisor.nio.com` 做 override → 10.112.238.232（nio.com 是公司自有域名，IT 可加），或验证 NAT hairpin 可用。二者至少其一，否则内网桌面浏览器点 IM 授权链接会在最后一步跳转失败。

### 4.2 auth 代码（小改）

- `--base-url` 拆两个：OAuth `redirect_uri` 用公网 base（新参 `--oauth-base-url` 之类），`/nsdp/submit`、`/jira/submit` 链接继续用内网 base（service.py:37/229/270 三处）。缺省回退同值，向后兼容，本地测试/集成测试零改动。
- 不拆的后果：submit 链接也会变成公网域名，PAT/NSDP token 提交面被推到公网，违背「仅 callback 公网」的初衷。

### 4.3 部署脚本与配置

- `prod-up.sh`：nginx 生成逻辑改双 server block；放开 line 11 的 `scheme == "http"` 断言（支持 https）；公网 block 加 `limit_req` 限流、只精确匹配 `/oauth/callback`（GET）、其余 404；公网面开启不含 query string 的审计日志（IP+status）。
- `prod.env` / `prod.env.example`：新增公网 base 与内网 base 两个变量（替代单一 `PUBLIC_BASE_URL` 的三重职责）。
- TLS 证书：IT 统一签发或 ACME（http-01 需 80 端口公网可达，与映射一并规划）。

### 4.4 飞书应用后台

- 回调注册改为 `https://meeting-advisor.nio.com/oauth/callback`（与 auth 拼出的值**精确匹配**）。
- 切换时点选无会议时段：存量 state TTL 仅 10 分钟，进行中的授权链接自然过期，无数据迁移。

### 4.5 文档同步

- DEPLOYMENT §2 组件表（回调行「域名可仅内网/VPN 解析」）、§3「v1 后端不接公网」基线声明、§7.1 前置条件与 §7.3 地址表/nginx 行。
- CONTRACT_AUTH §1 OAuth 面描述（`https://<域名>` → 公网域名口径）。
- **基线口径显式修订**：DEPLOYMENT「接公网前必须补 ingest TLS、服务间认证」一段，本次仅暴露 callback 一个只读端点（state 一次性 + PKCE S256 + GET + 不记 query 日志 + 限流），论证豁免并写入文档，而非默默违反。

## 5. 风险与验证

| # | 风险 | 验证/处置 |
|---|---|---|
| 1 | redirect_uri 全局唯一 → 内网桌面授权也跳公网域名，hairpin 失败会挂存量「入会授权」流程 | 切换前在内网 `curl https://meeting-advisor.nio.com/oauth/callback` 实测；要求 IT 内网 DNS override 或确认 hairpin |
| 2 | 公网暴露面扩大（扫描/爆破/滥用） | 仅一条精确路径 + 404 兜底 + limit_req + TLS；state 随机一次性、PKCE 已在位 |
| 3 | 手机提交 NSDP/Jira token 链接（内网域名）在公网打不开 | 现状即如此（内网域名手机本就打不开），非本次回退；后续需要时另行评估 |
| 4 | `prod-up.sh` 每次部署整体覆盖 nginx 配置 | 生成逻辑一次性改对即可，无手工残留 |
| 5 | GUI 默认服务器地址 `http://meeting-advisor.nioint.com` | 不变（GUI 走 `/gui/*` 内网面） |

## 6. 实施拆分（每项一个 commit）

1. auth 拆 `--oauth-base-url`（含单测：redirect_uri 与 submit 链接分源）。
2. `prod-up.sh` / `prod-run.sh` / `prod.env.example`：双 base 变量、双 server block、https 断言放开、限流与审计日志。
3. IT：公网 DNS + 映射 + 证书 + 内网 override；飞书后台回调注册更新。
4. 停机窗口（无活跃会话）：`prod-down.sh` → 改 `prod.env` → `prod-up.sh`；验收 §5 清单（内网桌面授权链接全流程 + 手机扫码 GUI 登录全流程）。
5. 文档回写（§4.5 四处 + 本文标注实施结果）。

## 7. 备选方案：单域名 + ACL（不推荐）

`meeting-advisor.nio.com` 单域名全用，nginx 对 `/gui/`、`/admin/`、`/nsdp/submit`、`/jira/submit` 加 `allow <内网网段>; deny all;`，callback 全开。

- 优点：零代码改动（仅 env + nginx）。
- 缺点：内网全部流量（GUI/submit/admin）依赖 hairpin；submit/GUI 链接挂公网域名，ACL 之外的可用性全押在网络策略上；与「仅 callback 公网」的边界表达不如双域名清晰。
