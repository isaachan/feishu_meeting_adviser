# 本地全组件真测指南（Mac 单机）

目标：在本地跑通**真实 OAuth（回调在 127.0.0.1）+ 真实日历校验 + 真实飞书 IM + 本地 STT + 假模型**的完整链路。对应计划 5.1/5.2 的本地部分；真实 GLM（5.3）可选。

## 1. 飞书开放平台（唯一需要云侧操作的部分）

1. **创建企业自建应用**（或复用旧 chatbot 验证过的应用）：<https://open.feishu.cn/app> → 记下 `App ID` / `App Secret`。
2. **添加机器人能力**：应用能力 → 机器人。
3. **权限**（权限管理，沿用旧 docs/APP_PERMISSIONS_EVENTS.md 已验证清单）：
   - `im:message`（接收与发送单聊消息）
   - `im:message:send_as_sb`（以应用身份发消息，如无则用默认发送权限）
   - `calendar:calendar:read`、`calendar:calendar.event:read`（日历读取，OAuth scope）
   - `offline_access`（refresh token）
4. **事件订阅**：订阅方式选 **长连接**（无需公网回调）；订阅事件 `im.message.receive_v1`。
5. **安全设置 → 重定向 URL**：精确添加
   ```
   http://127.0.0.1:8381/oauth/callback
   ```
   （本地回调；飞书接受 127.0.0.1 明文回调。注意：手机上点授权链接无法访问你 Mac 的 127.0.0.1，本地测试请在**电脑浏览器**完成授权。）
6. 发布应用版本并让测试账号可用（企业内可见即可）。

## 2. 素材准备

- **两个飞书账号**：小A（组织者）、小B（参会者），都能与 bot 单聊。
- **一场真实日历会议**：小A 是**组织者**，会议时间覆盖测试时段（时间窗 = 开始前 30 分钟 ～ 结束后 30 分钟），且**通过飞书日历创建的视频会议**（事件需带 vc 会议链接，校验按链接里的 9 位会议号匹配）。记下 9 位会议号。
- **sherpa STT 模型**（约 90MB）：
  ```bash
  mkdir -p ~/models && cd /tmp
  curl -fL --retry 2 -o stt-model.tar.bz2 \
    'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05.tar.bz2'
  echo 'fa5f63d618e5a01526e275a358bb7772e403f84808a4769fba52cffd8160bf74  stt-model.tar.bz2' | shasum -a 256 -c -
  tar xjf stt-model.tar.bz2 -C ~/models
  ```
- （可选，真实 GLM）本机 `opencode` 登录可用，见旧 `docs/REMOTE_SERVER_DEVELOPMENT.md`。

## 3. 配置与启动

```bash
cd /Users/kai.han/code/feishu_meeting_adviser
cp new/scripts/local.env.example new/scripts/local.env   # 填入凭据/模型路径
new/scripts/local-up.sh          # 首次会自动建 venv 并安装六个模块
new/scripts/local-down.sh        # 停止
```

端口：auth 8381 · advisor API 8380 · ingest 8400 · stt 8392 · gateway 8377。
日志：`new/data/local/logs/`；重置全部本地数据：`rm -rf new/data/local`。

## 4. 走查清单（S1–S5）

1. **首次授权（S1）**：飞书里小A 对 bot 发 `入会 <9位会议号>` → bot 回授权链接 → **电脑浏览器**打开完成飞书授权 → 回飞书**重发** `入会 <会议号>` → 收到 recorder 启动命令。
2. **采集**：新开终端粘贴执行命令（`--url ws://127.0.0.1:8400/ingest`）→ 开一场真的飞书会议并放声音（或本机播放音频）→ 观察上行统计行。
3. **回答（S2）**：说话数秒～数十秒内，小A 在飞书收到会参谋回答（当前为 fake 模型的"网关合成回答"；opencode 则为真实 GLM）。
4. **订阅（S3）**：小B 对 bot 发 `我想收到会议 <会议号> 的信息` → 小A 收到审批请求 → `同意 #N` → 之后小B 与小A 同时收到回答；`撤销 #N` 止血。
5. **收尾（S5）**：recorder 终端 Ctrl-C（不伪造收尾）→ 小A 发 `结束会议` → 云端排空至 ended；`状态` 查看概况。
6. **异常**：断网重连（recorder 自动重传）、bot/auth 重启不影响进行中会话。

## 5. 已知边界

- 本地假模型（`REMOTE_BACKEND=fake`）不产生真实"参谋"内容；切真实 GLM 用 `REMOTE_BACKEND=opencode`。
- 手机浏览器完成授权需要 auth 可从手机可达（本地栈不满足；正式部署走域名/TLS 反代）。
- 日历校验按事件 `vchat.meeting_url` 中的 9 位号匹配——日历里没有视频会议链接的事件无法通过会议号入会。
