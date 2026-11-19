# OpenCode serve 跨进程 session 续接：技术验证报告

状态：技术验证报告，2026-11-19，opencode 1.19.5（/opt/homebrew/bin/opencode）。一句话结论：**「进程冷、会话热」模型在 1.19.5 上成立**——serve 进程 kill -9 后，用同一 XDG 数据目录起新进程，可通过 HTTP API 找到旧 session、完整读回历史并继续对话，且历史上下文对后续轮次可见；同时存在可用的 prompt 中止端点（`POST /session/{id}/abort`），取消不必杀进程。本文验证 [T15 方案](LLM_GATEWAY_SESSION_REUSE.md)的前提，不改变任何现有代码。

## 1. 环境与方法

| 项 | 值 |
|---|---|
| opencode | 1.19.5，`opencode serve --pure --hostname 127.0.0.1 --port 0` |
| 模型 | provider `zai-coding-plan`，model `glm-5.3`（`/provider` 实际列出 `glm-5.3`、`glm-5.3-flash`、`glm-5.3-highspeed`，与 gateway 默认 `zai-coding-plan/glm-5.3` 一致，提交 prompt 时 providerID/modelID 按此切分） |
| 凭据 | 照 `opencode.py` `_environment()`：读 `~/.local/share/opencode/auth.json`，仅选 `zai-coding-plan` 一项构造 `OPENCODE_AUTH_CONTENT`（key 不打印、不落盘） |
| 启动复刻 | 私有 XDG 四目录（config/data/cache/state）、随机 `OPENCODE_SERVER_PASSWORD` + `meeting-adviser` basic auth、`OPENCODE_CONFIG_CONTENT` 照抄无 owner 分支（permission `*:deny`、mcp `{}`、plugin `[]`、compaction auto false、snapshot false、自定义 agent）、stdout 正则解析端口、`GET /global/health` 探活 |
| 存储 | 会话数据落在 `XDG_DATA_HOME/opencode/opencode.db`（SQLite，WAL 模式；表含 `session`/`message`/`part` 等），文件名不含 session id |
| 轮询 | 照 gateway：`GET /session/{id}/message`，找本轮 user message 的子 assistant 消息、`time.completed` 非空、无 tool part |

场景均为真实调用（暗号记忆法：第 1 轮告知暗号 X7Q2，后续轮次只问暗号）。进程 A/B 同一 XDG 根目录与同一 meeting cwd；共起 9 个短生命周期 serve 进程，全部回收。

### 计时汇总（冷启动成本，供 T15/T16 参考）

| 项 | 观测值 |
|---|---|
| 进程启动 → health OK | 0.52–0.53 s（4 次独立测量：0.527 / 0.525 / 0.515 / ~0.52） |
| 单轮小 prompt 完成 | 1.85–4.73 s（含重启后续接轮 2.35 s、中止后续轮 2.51 s、崩溃后续轮 3.23 s） |
| SIGTERM 退出 | 立即（<0.1 s，rc -15） |

## 2. 场景结果

| 场景 | 结论 |
|---|---|
| S1 同进程多轮 | PASS |
| S2 杀进程重启找会话 | PASS |
| S3 重启后续接 | PASS |
| S4 历史完整性 | PASS |
| S5 会话删除 | PASS |
| S6 取消/中止 | PASS（`/session/{id}/abort` 存在且可用） |
| S7 进行中崩溃 | PASS（观察性场景，状态可查、可续） |

### S1 基线：同进程两轮（§33 验证点 1）

进程 A 内 `POST /session` 建会话 → 第 1 轮「我的暗号是 X7Q2…」回答「已经记住」（4.73 s）→ 第 2 轮「我的暗号是什么？」回答 **X7Q2**（1.85 s，finish=stop）。同进程内多轮上下文可用。

### S2 杀进程重启找会话（核心）

kill -9 进程 A（rc=-9）→ 同一 XDG 目录起进程 B（health 0.525 s）→ `GET /session`：**旧 session 在列表首位**，返回字段含 `id`（`ses_…`）、`title`、`time{created,updated}`、`directory`（= meeting cwd）、`parentID`。磁盘上无按 session id 命名的文件，数据统一在 `opencode.db`（SQLite WAL）。

### S3 重启后续接（核心）

进程 B 中对旧 session `prompt_async` 再问「我的暗号是什么？」：提交成功、正常完成、回答 **X7Q2**（2.35 s）。崩溃重启后历史对模型可见可用。

### S4 历史完整性

进程 B `GET /session/{id}/message`：6 条消息，角色序列 user/assistant ×3，首轮 user 全文原样可读；assistant parts 为 `step-start/reasoning/text/step-finish`，`parentID` 链完整，跨重启的前两轮与重启后的第三轮无差别。

### S5 会话删除

`DELETE /session/{id}` 返回字面量 `true`；再 `GET /session` 已无该会话；SQLite `session` 表行数归零——**物理删除**，不是软删。T15 会议结束清理路径可用。

### S6 取消/中止

`GET /doc` 返回完整 OpenAPI 3.1 规范（162 个路径，可用作端点清单来源）。其中存在：

- `POST /session/{sessionID}/abort`（v1，本次实测）
- `POST /api/session/{sessionID}/interrupt`（v2，未实测）

实测：长 prompt 进行中（assistant 消息已建、未完成）调 abort → 返回 `true`；`GET /global/health` 仍 `{"healthy":true,"version":"1.19.5"}`；同 session 立即续问「只回复 OK」正常完成（2.51 s）。**进程不死、session 可继续**。

关键细节：被中止的 assistant 消息终态为 `time.completed` **已置位**、`finish: null`、`parts: []`（流式内容不保留）。gateway 式轮询（completed 非空 + 无 tool part 即取结果）会把这种空消息当作「已完成的有效回复」——T15 的结果解析必须同时排除「parts 为空 / finish 为 null」的中止态。

### S7 进行中崩溃

提交 prompt 后 0.14 s（user 消息已落库、assistant 行已建）kill -9 → 新进程重启后观察：

- 旧 session 仍在 `GET /session` 列表；
- 消息历史：本轮 user 消息**已持久化**（仅 `time.created`）；assistant 行存在但无 parts、无 `time.completed`——「已提交未确认」状态可从 API 精确识别；
- `GET /session/status` 返回 `{}`，无卡死的 busy 状态；
- 同 session 续问「只回复 OK」正常完成（3.23 s）。

对应 T15 恢复路径的观察结论：崩溃后按「本轮 user message 是否已落 + 其子 assistant 是否 completed」即可区分未提交/已提交未完成/已完成，无需废弃会话。

## 3. 附带发现

1. **端口行为**：`--port 0` 实测优先绑定默认端口 **4096**；4096 被占时回落到随机端口（观测到 49588），并发两个 serve 无冲突。gateway 从 stdout 解析端口的写法不受影响，但不要假设 `--port 0` 等于「随机端口」。
2. **目录作用域**：session 自带 `directory` 字段，但 v1 `GET /session` 列表与 `GET /session/{id}` 直查在换 cwd 后仍能访问同 DB 中的旧会话（未观察到按 cwd 硬隔离）。T15 的跨会议隔离不能依赖 cwd，仍须依赖每会议独立 XDG 目录（方案第 2 条不变）；同时建议续接时保持会议 cwd 稳定，避免依赖未验证的跨目录行为。
3. **本机代理**：环境 http_proxy 会劫持对 127.0.0.1 的 urllib 请求（Squid 503）。gateway 已用 `ProxyHandler({})` 禁用代理；自写脚本同样必须禁用。

## 4. 对 T15 的影响

已化解的前提问题：

- **重启恢复**：同 XDG + 同 cwd 重启后，会话可列出、历史完整、续问可用——「进程冷、会话热」的资源模型（每轮任务起进程、会话绑定会议数据目录）可行，进程可按需冷启停，会议数据横跨进程生命周期。
- **取消语义**：不必杀进程。`POST /session/{id}/abort` 中止当前 prompt 且 session 立即可用；需在结果解析中识别中止态（completed 置位 + parts 空 + finish null）。
- **清理路径**：`DELETE /session/{id}` 物理删除，磁盘随之清空，满足会议结束回收。
- **崩溃恢复**：「已提交未确认」可通过消息持久化状态精确判定（§S7），为幂等重发提供了比预期更干净的基础。

仍然存在的问题（与本次验证无关，照旧）：

- **增量×幂等协议缺口**：每轮输入格式、`fresh_input_keys` 传递与本轮 user message id 记录仍需按方案实现；gateway 现有 `_decide_once` 假设 session 至多一条 user message，复用前必须改为按轮定位（方案已列）。
- **同会议排队**：同一 session 串行、排队/执行期限分离的设计不变。
- 新增注意：跨会议隔离必须坚持每会议独立 XDG（目录作用域不隔离，§3.2）；压缩行为未验证（本验证 compaction auto false），长会议上下文预算仍按方案先行验证。

## 5. 局限性

- 单机（macOS）、单次样本、9 个进程、7 个模型轮次；未覆盖并发同 session 提交。
- 无 MCP/工具环境（deny-all、mcp 空），tool part 的持久性与重启后可见性未验证。
- 结论绑定 opencode 1.19.5 与 `/doc` 所列 API；端口回落、目录作用域等行为属观测而非文档承诺，升级版本需复验。
- 未覆盖长会话上下文增长、压缩/重建（compaction 关闭）、多 provider。

## 附录：最小复现脚本（脱敏）

auth 读取仅选取 `zai-coding-plan` 项且不输出；服务器密码随机生成、仅存内存。scratch 运行目录与 XDG 根均为一次性目录。

```python
# oc_resume_probe.py — opencode 1.19.5 serve 跨进程 session 续接最小验证
import base64, json, os, queue, re, secrets, signal, subprocess, threading, time
from pathlib import Path
from urllib import request

PROVIDER, AGENT = 'zai-coding-plan', 'meeting-decision'
SCRATCH = Path('/tmp/oc-resume')          # 一次性根目录
XDG, CWD = SCRATCH / 'xdg', SCRATCH / 'meeting'
AUTH = Path.home() / '.local/share/opencode/auth.json'

def auth_content():                        # 只选目标 provider,绝不打印
    c = json.loads(AUTH.read_text())[PROVIDER]
    assert c.get('type') == 'api' and c.get('key')
    return json.dumps({PROVIDER: {'type': 'api', 'key': c['key']}})

def config(model_id):
    perm = {'*': 'deny'}
    return {'model': f'{PROVIDER}/{model_id}', 'enabled_providers': [PROVIDER],
            'permission': perm, 'share': 'disabled', 'autoupdate': False, 'snapshot': False,
            'compaction': {'auto': False}, 'plugin': [], 'mcp': {}, 'instructions': [],
            'agent': {AGENT: {'mode': 'primary', 'permission': perm, 'steps': 2,
                              'prompt': '你是会议助手。禁用一切工具,按指示简短回答。'}}}

class Server:
    def __init__(self, model_id):
        self.model_id, self.password = model_id, secrets.token_urlsafe(24)
    def start(self):
        env = os.environ.copy()
        for n in [k for k in env if k.startswith('OPENCODE_')]: del env[n]
        CWD.mkdir(parents=True, exist_ok=True)
        for c in ('config', 'data', 'cache', 'state'):
            (XDG / c).mkdir(parents=True, exist_ok=True)
            env['XDG_' + c.upper() + '_HOME'] = str(XDG / c)
        env.update({'OPENCODE_AUTH_CONTENT': auth_content(),
                    'OPENCODE_SERVER_PASSWORD': self.password,
                    'OPENCODE_SERVER_USERNAME': 'meeting-adviser',
                    'OPENCODE_CONFIG_CONTENT': json.dumps(config(self.model_id))})
        for f in ('DISABLE_AUTOUPDATE', 'DISABLE_CLAUDE_CODE', 'DISABLE_EXTERNAL_SKILLS',
                  'DISABLE_DEFAULT_PLUGINS', 'DISABLE_PROJECT_CONFIG',
                  'DISABLE_LSP_DOWNLOAD', 'DISABLE_SHARE'):
            env['OPENCODE_' + f] = 'true'
        t0 = time.monotonic()
        self.proc = subprocess.Popen(                       # bufsize=0: 立即读到端口行
            ['/opt/homebrew/bin/opencode', 'serve', '--pure',
             '--hostname', '127.0.0.1', '--port', '0'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(CWD), env=env, bufsize=0)
        origins, buf = queue.Queue(maxsize=1), b''
        def drain():
            b = b''
            try:
                while chunk := self.proc.stdout.read(4096):
                    b = (b + chunk)[-16384:]
                    m = re.search(rb'http://127\.0\.0\.1:([0-9]{1,5})(?:\s|$)', b)
                    if m and origins.empty():
                        origins.put_nowait('http://127.0.0.1:' + m.group(1).decode())
            except Exception: pass
        threading.Thread(target=drain, daemon=True).start()
        self.origin = origins.get(timeout=90)
        while time.monotonic() - t0 < 90:
            try:
                if self.req('GET', '/global/health', timeout=10).get('healthy'):
                    self.start_s = round(time.monotonic() - t0, 3); return
            except Exception: time.sleep(0.05)
        raise RuntimeError('health failed')
    def req(self, method, path, payload=None, timeout=30.0):
        r = request.Request(self.origin + path,
            data=None if payload is None else json.dumps(payload).encode(), method=method,
            headers={'Authorization': 'Basic ' + base64.b64encode(
                ('meeting-adviser:' + self.password).encode()).decode(),
                'Content-Type': 'application/json'})
        opener = request.build_opener(request.ProxyHandler({}))   # 本机代理必须禁用
        with opener.open(r, timeout=timeout) as resp:
            raw = resp.read(); return json.loads(raw) if raw else None
    def kill9(self):
        self.proc.send_signal(signal.SIGKILL); self.proc.wait()

def round_trip(srv, sid, known_users, text, timeout=240):
    srv.req('POST', f'/session/{sid}/prompt_async',
            {'agent': AGENT, 'model': {'providerID': PROVIDER, 'modelID': srv.model_id},
             'tools': {t: False for t in srv.req('GET', '/experimental/tool/ids')},
             'parts': [{'type': 'text', 'text': text}]})
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        msgs = srv.req('GET', f'/session/{sid}/message')
        users = {m['info']['id'] for m in msgs if m['info'].get('role') == 'user'}
        new = users - known_users
        if new:
            uid = sorted(new)[0]
            for m in msgs:
                i = m['info']
                if i.get('role') == 'assistant' and i.get('parentID') == uid \
                        and i.get('time', {}).get('completed') \
                        and not any(p.get('type') == 'tool' for p in m['parts']):
                    return uid, ''.join(p['text'] for p in m['parts'] if p.get('type') == 'text')
        time.sleep(0.25)
    raise TimeoutError

a = Server('glm-5.3'); a.start()                    # 进程 A
sid = a.req('POST', '/session', {'title': 'resume probe',
    'permission': [{'permission': '*', 'pattern': '*', 'action': 'deny'}]})['id']
u1, _ = round_trip(a, sid, set(), '我的暗号是 X7Q2。请记住。只回复:已经记住。')
users = {m['info']['id'] for m in a.req('GET', f'/session/{sid}/message')
         if m['info'].get('role') == 'user'}
u2, ans = round_trip(a, sid, users, '我的暗号是什么?只回复暗号本身。')
assert ans == 'X7Q2', ans                           # S1: 同进程多轮 PASS
a.kill9()                                           # 模拟崩溃
b = Server('glm-5.3'); b.start()                    # 进程 B,同一 XDG + 同一 cwd
assert any(e['id'] == sid for e in b.req('GET', '/session'))   # S2 PASS
users = {m['info']['id'] for m in b.req('GET', f'/session/{sid}/message')
         if m['info'].get('role') == 'user'}
u3, ans = round_trip(b, sid, users, '我的暗号是什么?')          # S3 PASS
assert ans == 'X7Q2', ans
assert b.req('DELETE', f'/session/{sid}') is True               # S5 PASS
b.proc.terminate(); b.proc.wait()
```
