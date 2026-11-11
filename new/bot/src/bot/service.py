"""Bot service: inbound command handling + outbox notification polling.

IM transport and advisor client are injected (stub-first); lark wiring lives
in lark_bot.py. Message handling is idempotent per message_id; the outbox
poller is a single sequential worker (ported chatbot worker semantics).
"""
import logging
import threading
import time
from typing import Protocol

from .advisor_client import AdvisorApiError, AdvisorClient
from .commands import Command, CommandError, parse_command
from .recorder_command import CommandRenderError, render

LOG = logging.getLogger(__name__)

HELP = ('会参谋指令：会议列表 / 入会 <会议号|链接> / 结束会议 [会议号] / 状态 / '
        '同意 [编号] / 拒绝 [编号]（不带编号=最新待审批）/ 撤销 [编号]（撤销已批准的订阅）。'
        '参会者可发：我想收到会议 <会议号> 的信息 / 取消订阅 [会议号]')


class ImTransport(Protocol):
    def send_dm(self, open_id: str, text: str) -> str: ...
    def send_reply(self, message_id: str, text: str) -> str: ...


class BotService:
    def __init__(self, advisor: AdvisorClient, transport: 'ImTransport | None' = None, *,
                 poll_interval=1.0, outbox_limit=10, clock=time.time):
        self.advisor = advisor
        self.transport = transport
        self.poll_interval = poll_interval
        self.outbox_limit = outbox_limit
        self.clock = clock
        self.seen: dict[str, None] = {}
        self._seen_guard = threading.Lock()
        self._stop = threading.Event()
        self._poller = None

    # -- inbound ---------------------------------------------------------------

    def handle_message(self, sender_open_id: str, text: str, message_id: str,
                       *, sender_name: str = '') -> None:
        with self._seen_guard:
            if message_id in self.seen:
                return
            self.seen[message_id] = None
        try:
            command = parse_command(text)
        except CommandError as exc:
            self._reply(message_id, str(exc))
            return
        if command is None:
            self._reply(message_id, HELP)
            return
        try:
            handler = getattr(self, f'_cmd_{command.kind}')
            handler(sender_open_id, command, message_id)
        except AdvisorApiError as exc:
            self._reply(message_id, self._advisor_error_text(exc))
        except CommandRenderError as exc:
            LOG.warning('command render failed: %s', exc)
            self._reply(message_id, f'采集命令渲染失败（{exc}）；请联系管理员')

    def _reply(self, message_id, text):
        try:
            if self.transport is not None:
                self.transport.send_reply(message_id, text)
        except Exception:
            LOG.exception('reply failed')

    def _advisor_error_text(self, exc: AdvisorApiError) -> str:
        if exc.code == 'authorization_required':
            url = exc.extra.get('authorize_url', '')
            return f'需要先完成授权，请点击链接（电脑/手机均可）：\n{url}'
        if exc.code == 'meeting_not_found':
            return '未在日历中找到这场会议'
        if exc.code == 'not_organizer':
            return '你不是这场日历会议的组织者'
        if exc.code == 'outside_window':
            return '当前不在会议时间窗内'
        if exc.code == 'not_found':
            return '未找到该会议或会议已结束'
        if exc.code == 'invalid_state':
            if 'revoked' in (exc.message or ''):
                return '该订阅已被组织者撤销'
            return exc.message or '当前状态不允许该操作'
        if exc.code == 'not_owner':
            return '只有组织者可以执行该操作'
        if exc.code in ('advisor_unreachable', 'auth_unavailable'):
            return '服务暂不可用，请稍后再试'
        return '操作失败：' + (exc.message or exc.code)

    # -- command handlers --------------------------------------------------------

    def _cmd_join(self, sender, command, message_id):
        result = self.advisor.create_session(
            sender, command.meeting_no, password=command.password,
            message_id=message_id)
        args = dict(result['recorder_command_args'])
        args['mic'] = bool(command.kind == 'join' and False)  # mic opt-in lands with S1 UX
        command_text = render(args)
        hint = '（该会议已在服务中，沿用原会话）' if result.get('existing') else ''
        self._reply(message_id,
                    f'已入会《{result["title"]}》{hint}\n在 laptop 终端粘贴执行：\n\n'
                    f'{command_text}\n\n启动后我会开始听会。')

    def _cmd_end(self, sender, command, message_id):
        listing = self.advisor.list_sessions(sender).get('sessions', [])
        target = None
        if command.meeting_no:
            target = next((s for s in listing if s['meeting_no'] == command.meeting_no), None)
            if target is None:
                self._reply(message_id, '未找到该会议的进行中会话')
                return
        elif listing:
            target = listing[0]
        if target is None:
            self._reply(message_id, '当前没有进行中的会话')
            return
        result = self.advisor.end_session(target['session_id'], sender,
                                          message_id=message_id)
        phase = result['phase']
        text = '会话已结束。' if phase == 'ended' else '正在排空收尾（draining），在途回答发完即结束。'
        self._reply(message_id, text)

    def _pending_request_no(self, owner, state='requested'):
        newest = None
        for session in self.advisor.list_sessions(owner).get('sessions', []):
            status = self.advisor.session_status(session['session_id'])
            for row in status.get('subscriptions', []):
                if row['state'] == state and (newest is None
                                              or row['request_no'] > newest):
                    newest = row['request_no']
        return newest

    def _cmd_approve(self, sender, command, message_id):
        number = command.request_no or self._pending_request_no(sender)
        if number is None:
            self._reply(message_id, '当前没有待审批的订阅申请。')
            return
        result = self.advisor.decide_subscription(number, sender, 'approve',
                                                  message_id=message_id)
        self._reply(message_id, f'已批准订阅 #{result["request_no"]}，之后的回答将同时发送给对方。')

    def _cmd_deny(self, sender, command, message_id):
        number = command.request_no or self._pending_request_no(sender)
        if number is None:
            self._reply(message_id, '当前没有待审批的订阅申请。')
            return
        result = self.advisor.decide_subscription(number, sender, 'deny',
                                                  message_id=message_id)
        self._reply(message_id, f'已拒绝订阅 #{result["request_no"]}。')

    def _cmd_revoke(self, sender, command, message_id):
        if not command.request_no:
            approved = []
            for session in self.advisor.list_sessions(sender).get('sessions', []):
                status = self.advisor.session_status(session['session_id'])
                approved += [row['request_no'] for row in status.get('subscriptions', [])
                             if row['state'] == 'approved']
            if len(approved) == 1:
                command = Command('revoke', request_no=approved[0])
            elif not approved:
                self._reply(message_id, '当前没有已批准的订阅。')
                return
            else:
                self._reply(message_id,
                            '有多条已批准订阅，请指明编号：' +
                            '、'.join(f'#{number}' for number in sorted(approved)))
                return
        result = self.advisor.revoke_subscription(command.request_no, sender,
                                                  message_id=message_id)
        self._reply(message_id, f'已撤销订阅 #{result["request_no"]}，对方将不再收到回答。')

    def _cmd_unsubscribe(self, sender, command, message_id):
        """Subscriber-side opt-out: cancel my own approved subscription."""
        pending = self.advisor.my_subscriptions(sender).get('subscriptions', [])
        if command.meeting_no:
            pending = [row for row in pending if row['meeting_no'] == command.meeting_no]
        approved = [row for row in pending if row['state'] == 'approved']
        if not approved:
            self._reply(message_id, '你没有可取消的订阅。')
            return
        if len(approved) > 1:
            listing = '、'.join(f'#{row["request_no"]}（会议 {row["meeting_no"]}）'
                               for row in approved)
            self._reply(message_id, f'你有多条订阅，请指明会议号：{listing}')
            return
        row = approved[0]
        result = self.advisor.revoke_subscription_by_subscriber(row['request_no'], sender,
                                                                message_id=message_id)
        self._reply(message_id, f'已取消订阅 #{result["request_no"]}，不再接收该会议的回答。')

    def _cmd_meetings(self, sender, command, message_id):
        result = self.advisor.list_meetings(sender)
        rows = result.get('meetings', [])
        if not rows:
            self._reply(message_id, '近期没有查到带视频会议链接的日历会议。')
            return
        import datetime
        lines = []
        for row in rows:
            start = datetime.datetime.fromtimestamp(row['start_ts'])
            end = datetime.datetime.fromtimestamp(row['end_ts'])
            span = (f'{start.strftime("%m-%d %H:%M")}-{end.strftime("%H:%M")}'
                    if start.date() == end.date()
                    else f'{start.strftime("%m-%d %H:%M")} ~ {end.strftime("%m-%d %H:%M")}')
            no = row.get('meeting_no')
            no_text = f'会议号 {no}' if no else '无会议号'
            flags = []
            if row.get('in_progress'):
                flags.append('进行中')
            flags.append('你是组织者' if row.get('is_organizer') else '非组织者')
            lines.append(f'《{row["title"]}》 {no_text} · {span} · {" · ".join(flags)}')
        self._reply(message_id, '近期会议（入会请直接发：入会 <会议号>）：\n' + '\n'.join(lines))

    def _cmd_status(self, sender, command, message_id):
        listing = self.advisor.list_sessions(sender).get('sessions', [])
        if not listing:
            self._reply(message_id, '当前没有进行中的会话。')
            return
        lines = []
        for session in listing:
            status = self.advisor.session_status(session['session_id'])
            state_text = {'requested': '待审批', 'approved': '已批准',
                          'denied': '已拒绝', 'revoked': '已撤销'}
            subs = [f'#{s["request_no"]} {state_text.get(s["state"], s["state"])}'
                    for s in status.get('subscriptions', [])]
            lines.append(f'《{session["title"]}》 {session["phase"]} · '
                         f'模型 {status.get("model_state", "?")} · 订阅 {" ".join(subs) or "无"}')
        self._reply(message_id, '\n'.join(lines))

    def _cmd_subscribe(self, sender, command, message_id):
        result = self.advisor.create_subscription(sender, command.meeting_no,
                                                  message_id=message_id)
        if result.get('existing'):
            self._reply(message_id, '你已订阅该会议的回答。')
            return
        request_no = result['request_no']
        owner = result['owner_open_id']
        title = result.get('title') or command.meeting_no
        self._reply(message_id, f'已向组织者申请订阅《{title}》（#{request_no}），等待审批。')
        try:
            if self.transport is None:
                raise RuntimeError('no im transport wired')
            self.transport.send_dm(
                owner, f'有参会者申请接收会议《{title}》的会参谋回答。\n'
                       f'回复「同意 #{request_no}」或「拒绝 #{request_no}」。')
        except Exception:
            LOG.exception('approval request delivery failed (best-effort by design)')

    # -- outbox poller -------------------------------------------------------------

    def start_polling(self):
        if self._poller is not None:
            return
        self._stop.clear()

        def worker():
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    LOG.exception('outbox poll failed')
                self._stop.wait(self.poll_interval)

        self._poller = threading.Thread(target=worker, name='bot-outbox', daemon=True)
        self._poller.start()

    def stop_polling(self, timeout=5):
        self._stop.set()
        if self._poller is not None:
            self._poller.join(timeout=timeout)

    def poll_once(self):
        pending = self.advisor.pending_outbox(limit=self.outbox_limit).get('rows', [])
        for row in pending:
            open_id = row.get('recipient_open_id')
            text = (row.get('payload') or {}).get('text', '')
            if not open_id or not text:
                continue
            try:
                if self.transport is None:
                    raise RuntimeError('no im transport wired')
                self.transport.send_dm(open_id, text)
            except Exception as exc:
                self.advisor.outbox_result(row['uuid'], 'uncertain',
                                           error=exc.__class__.__name__)
                continue
            self.advisor.outbox_result(row['uuid'], 'sent')
