# 产品接入依据与未验证行为

核验日期：2026-10-13。读取飞书公开 Markdown 文档，并结合 [STAGE0_FINDINGS.md](STAGE0_FINDINGS.md) 的脱敏观察；文档可读不等于当前会议行为已经验收。

- [会议事件列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/events.md)：支持应用身份，应用机器人先入会；请求可携带起止 Unix 秒和分页大小，频率上限为每分钟 100 次；下一页标记用于继续遍历。
- [加入会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/join.md)、[离开会议](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/leave.md)：入会返回内部会议 ID 和机器人参会者信息，离会以内部 ID 请求。

产品每次扫描固定起止范围和分页大小，整轮完成前不更换参数；只有字幕与检查点事务提交成功才续页。页预算耗尽后下轮续读，循环游标停止推进并报告错误。扫描完成边界根据请求结束时间推进，独立于最大事件时间。普通扫描重叠 120 秒，默认每 60 秒从首次读取范围起点重新核对；重新核对与恢复回补标为历史输入，持久化去重避免重复上下文。大会议回扫可能增加追赶时间，需真实高事件量验收，20 秒目标尚未通过。

实测为顶层字幕事件，文档还描述嵌套活动事件；产品兼容两者及字符串/对象 payload。默认不保存整个 payload。字幕起止时间兼容文档数字毫秒与实测 RFC 3339。句子 ID 为主要身份；缺失时按事件 ID + 项序号回退，均缺失时按正文、说话人及时间指纹回退。没有 ID 且内容/身份/时间完全相同的不同发言无法可靠区分，必须标记降级。

**仍未知：**事件排序及时间过滤对应哪种业务时间、边界包含关系、迟到事件可否追溯、游标有效期与跨进程稳定性、历史留存、稳定快照语义、字幕修订版本及准确的会议结束事件枚举。固定窗口是受控扫描策略，不能称作服务端快照保证。文档没有独立游标失效错误码，不能把通用参数错误直接宣称为游标过期。

首读默认回看 10 分钟，之前的内容不在自动采集范围内；之后保留初始边界做周期核对。缺失时间只标记未知，不补成接收时间。没有已核实的修订版本时，同句变化进入冲突状态并保留候选，不按事件时间猜测版本。上游已不可回查或完整性无法核实的恢复区间保持不完整状态。真实多页、空页边界与跨会议恢复仍由 1.9 留下授权实测证据。

认证依据：[自建应用 token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal.md) 的返回有效期，以及 [通用错误码](https://open.feishu.cn/document/ukTMukTMukTM/ugjM14COyUjL4ITN.md) 中的认证失效、权限和限流分类。客户端最多进行一次认证刷新重放；入会/离会的网络超时不自动重发。通用参数错误伴随已保存游标时，仅允许一次保持原查询范围、去掉游标的受控重扫，并永久保留完整性风险；后续仍失败则需人工处理。文档明确要求人工处理的入离会服务错误不进入持续重试。

离会错误 `121104`（meeting status unexpected）的官方解释是会议当前状态不支持机器人离会，需确认会议状态后重试；它不等同于 `121110`（机器人不在会议中）。产品保留原会话并提示人工确认：仅在旧会议已结束或确认 bot 已离会时使用 `reset-session --confirmed-bot-left`，再加入新会议。依据：[离开会议错误码](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/vc-v1/bot/leave.md)。
