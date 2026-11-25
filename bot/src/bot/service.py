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
from .render import OutboundMessage

LOG = logging.getLogger(__name__)


def _message(text, format='text') -> OutboundMessage:
    return text if isinstance(text, OutboundMessage) else OutboundMessage(text, format)


HELP = ('你可以这样发消息：\n'
        '会议列表\n'
        '入会 123456789\n'
        '结束会议（也可加会议号）\n'
        '状态\n'
        '同意 #7\n'
        '拒绝 #7\n'
        '撤销 #7（取消已批准的订阅）\n'
        '我想收到会议 123456789 的信息\n'
        '取消订阅（也可加会议号）\n'
        '同意、拒绝不带编号时，处理最新申请。')


class ImTransport(Protocol):
    def send_dm(self, open_id: str, message: OutboundMessage) -> str: ...
    def send_reply(self, message_id: str, message: OutboundMessage) -> str: ...


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
        self._user_locks: dict[str, threading.Lock] = {}
        self._user_locks_guard = threading.Lock()
        self._stop = threading.Event()
        self._poller = None

    # -- slow-command background execution (T6) ---------------------------------
    # 入会等慢指令：立即回执，advisor 调用放后台线程，完成后主动 DM 结果。
    # 每用户一把锁串行其慢指令（重发入会在 advisor 侧本就幂等）；顺序 worker
    # 不再被单个慢指令阻塞，其他用户/指令即时处理。

    def _user_lock(self, open_id: str) -> threading.Lock:
        with self._user_locks_guard:
            lock = self._user_locks.get(open_id)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[open_id] = lock
            return lock

    def _spawn_slow(self, lock, sender, work):
        def guarded():
            with lock:
                try:
                    work()
                except AdvisorApiError as exc:
                    LOG.warning('slow command failed: %s %s', exc.code, exc.message)
                    self._dm(sender, self._advisor_error_text(exc))
                except Exception:
                    LOG.exception('slow command crashed')
                    self._dm(sender, '操作失败，请稍后重试')
        threading.Thread(target=guarded, name='bot-slow', daemon=True).start()

    def _dm(self, open_id, text, *, format='text'):
        try:
            if self.transport is not None:
                self.transport.send_dm(open_id, _message(text, format))
        except Exception:
            LOG.exception('dm failed')

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
            LOG.warning('advisor error: %s %s (%s)', exc.code, exc.message,
                        exc.extra if exc.code in ('authorization_required',
                                                  'advisor_unreachable') else '')
            self._reply(message_id, self._advisor_error_text(exc))

    def _reply(self, message_id, text, *, format='text'):
        try:
            if self.transport is not None:
                self.transport.send_reply(message_id, _message(text, format))
        except Exception:
            LOG.exception('reply failed')

    def _advisor_error_text(self, exc: AdvisorApiError) -> str:
        if exc.code == 'authorization_required':
            missing = exc.extra.get('missing_credentials')
            if isinstance(missing, list) and missing:
                lines = ['入会前，请先完成以下操作：']
                labels = {'feishu': '授权飞书', 'nsdp': '提交版本计划凭据',
                          'jira': '提交 Jira 凭据'}
                for item in missing:
                    if not isinstance(item, dict) or item.get('provider') not in labels:
                        continue
                    url = item.get('authorize_url') or item.get('submit_url')
                    if isinstance(url, str) and url:
                        lines.append(f"{labels[item['provider']]}：{url}")
                lines.append('链接 10 分钟内有效。完成后，请重新发送「入会」。')
                return '\n'.join(lines)
            if exc.extra.get('provider') == 'jira':
                return ('请打开链接提交 Jira 凭据（10 分钟内有效）：\n'
                        + exc.extra.get('submit_url', ''))
            if exc.extra.get('provider') == 'nsdp':
                url = exc.extra.get('submit_url', '')
                return ('请打开链接提交版本计划凭据（10 分钟内有效）：\n' + url)
            url = exc.extra.get('authorize_url', '')
            return f'请先打开链接完成飞书授权：\n{url}'
        if exc.code == 'nsdp_unavailable':
            return '版本计划暂时无法查询，请稍后重试。'
        if exc.code == 'meeting_not_found':
            return '未在日历中找到这场会议'
        if exc.code == 'not_organizer':
            return '你不是这场日历会议的组织者'
        if exc.code == 'meeting_ambiguous':
            return '该会议号当前对应多个场次，请稍后在目标场次的时间窗内重试'
        if exc.code == 'session_claimed':
            return '这场会议的会参谋已由其他人开启。'
        if exc.code == 'instance_ended':
            return '这场会议的会参谋已结束，无法再次开启。'
        if exc.code == 'claim_conflict':
            title = exc.extra.get('conflicting_title')
            meeting = f'（「{title}」）' if title else ''
            return f'你已有一场会参谋会议{meeting}，请先结束或退出。'
        if exc.code == 'not_supervisor':
            return '只有会议组织者或获授权的参会者可以开启会参谋。'
        if exc.code == 'not_invited':
            return '你不在这场会议的邀请名单中，无法开启会参谋。'
        if exc.code == 'outside_window':
            return '现在还不能入会，请在会议开始前后再试。'
        if exc.code == 'not_found':
            return '未找到该会议或会议已结束'
        if exc.code == 'invalid_state':
            if 'revoked' in (exc.message or ''):
                return '该订阅已被组织者撤销'
            if 'expired' in (exc.message or ''):
                return '这条订阅申请已过期，请重新申请。'
            return '现在无法完成这个操作，请查看「状态」后再试。'
        if exc.code == 'not_owner':
            return '只有这场会参谋会议的负责人可以操作。'
        if exc.code == 'advisor_unreachable':
            return '暂时没有收到结果。请等 1 分钟后发送「状态」确认，先不要重复操作。'
        if exc.code == 'auth_unavailable':
            return '授权服务暂时不可用，请稍后重试。'
        return '操作没成功，请稍后重试。'

    # -- command handlers --------------------------------------------------------

    def _cmd_join(self, sender, command, message_id):
        """慢指令：立即回执，建会放后台，完成后主动通知。"""
        self._reply(message_id,
                    '正在入会…首次可能需要几十秒，期间无需重发。')
        self._spawn_slow(self._user_lock(sender), sender,
                         lambda: self._join_now(sender, command, message_id))

    def _join_now(self, sender, command, message_id):
        result = self.advisor.create_session(
            sender, command.meeting_no, password=command.password,
            message_id=message_id)
        self._dm(sender, f'已入会「{result["title"]}」。\n请确保会参谋客户端已开启。')

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
        text = (f'「{target["title"]}」已结束。' if phase == 'ended'
                else f'正在结束「{target["title"]}」，处理完成后会自动结束。')
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
        """Withdraw a pending request or cancel an approved subscription."""
        pending = self.advisor.my_subscriptions(sender).get('subscriptions', [])
        if command.meeting_no:
            pending = [row for row in pending if row['meeting_no'] == command.meeting_no]
        active = [row for row in pending if row['state'] in ('requested', 'approved')]
        if not active:
            self._reply(message_id, '你没有可取消的订阅。')
            return
        if len(active) > 1:
            listing = '、'.join(f'#{row["request_no"]}（会议 {row["meeting_no"]}）'
                               for row in active)
            self._reply(message_id, f'你有多条订阅，请指明会议号：{listing}')
            return
        row = active[0]
        result = self.advisor.revoke_subscription_by_subscriber(row['request_no'], sender,
                                                                 message_id=message_id)
        self._reply(message_id, f'已撤回待审批申请 #{result["request_no"]}，名额已释放。'
                    if result['state'] == 'subscriber_cancelled' and row['state'] == 'requested'
                    else f'已取消订阅 #{result["request_no"]}，不再接收该会议的回答。')

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
        self._reply(message_id, '近期会议（例如：入会 123456789）：\n' + '\n'.join(lines))

    def _cmd_status(self, sender, command, message_id):
        listing = self.advisor.list_sessions(sender).get('sessions', [])
        if not listing:
            self._reply(message_id, '当前没有进行中的会话。')
            return
        lines = []
        for session in listing:
            status = self.advisor.session_status(session['session_id'])
            state_text = {'requested': '待审批', 'approved': '已批准',
                          'denied': '已拒绝', 'revoked': '已撤销',
                          'expired': '已过期', 'subscriber_cancelled': '申请人已撤回',
                          'owner_revoked': '已撤销'}
            phase_text = {'running': '进行中', 'paused': '已暂停',
                          'draining': '结束中', 'ended': '已结束'}
            subs = [f'#{s["request_no"]} {state_text.get(s["state"], "已结束")}'
                    for s in status.get('subscriptions', [])]
            lines.append(f'「{session["title"]}」：{phase_text.get(session["phase"], "状态未知")}\n'
                         f'订阅：{"、".join(subs) if subs else "无"}')
        self._reply(message_id, '\n'.join(lines))

    def _cmd_subscribe(self, sender, command, message_id):
        result = self.advisor.create_subscription(sender, command.meeting_no,
                                                  message_id=message_id)
        if result.get('existing'):
            self._reply(message_id,
                        f'申请 #{result["request_no"]} 已在等待审批。'
                        if result['state'] == 'requested' else '你已订阅该会议的回答。')
            return
        request_no = result['request_no']
        owner = result['owner_open_id']
        title = result.get('title') or command.meeting_no
        meeting_no = result.get('meeting_no') or command.meeting_no
        self._reply(message_id, f'已提交「{title}」的订阅申请，等待会议负责人同意。')
        try:
            if self.transport is None:
                raise RuntimeError('no im transport wired')
            applicant = result.get('subscriber_display') or sender
            self.transport.send_dm(
                owner, OutboundMessage(
                    f'{applicant} 申请接收「{title}」（会议号 {meeting_no}）的会参谋回答。\n'
                    f'回复「同意 #{request_no}」或「拒绝 #{request_no}」。', 'text'))
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
            payload = row.get('payload') or {}
            text = payload.get('text', '')
            format = payload.get('format', 'text')
            if not open_id or not text:
                continue
            message = _message(text, format)
            # Two-phase send (STATE_MACHINES v2 §1.3): claim `sending` before
            # touching Feishu so a crash mid-send lands in recoverable state
            # instead of an untracked duplicate.
            try:
                self.advisor.outbox_result(row['uuid'], 'sending')
            except AdvisorApiError as exc:
                LOG.info('outbox claim skipped: uuid=%s (%s)', row['uuid'], exc.code)
                continue
            LOG.info('outbox delivering: uuid=%s -> %s format=%s text="%s"', row['uuid'],
                     open_id, message.msg_format, text[:120])
            try:
                if self.transport is None:
                    raise RuntimeError('no im transport wired')
                self.transport.send_dm(open_id, message)
            except Exception as exc:
                LOG.warning('outbox delivery failed: uuid=%s -> %s (%s)',
                            row['uuid'], open_id, exc.__class__.__name__)
                self.advisor.outbox_result(row['uuid'], 'uncertain',
                                           error=exc.__class__.__name__)
                continue
            LOG.info('outbox delivered: uuid=%s -> %s', row['uuid'], open_id)
            self.advisor.outbox_result(row['uuid'], 'sent')
