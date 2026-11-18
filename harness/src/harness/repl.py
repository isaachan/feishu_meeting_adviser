"""Interactive REPL: accumulate simulated captions, /decide against the gateway."""
import json
import time

from . import __version__
from .client import GatewayClient, GatewayError
from .decision import validate_decision
from .session import LOCAL_SPEAKER, MeetingSession

LOCAL_SPEAKER_PREFIXES = {'me', '我', '本机'}
REMOTE_SPEAKER_PREFIXES = {'其他', '别的', '远程', 'other', 'remote'}

BANNER = """llm-harness {version}  gateway={url} backend={backend} deadline={deadline}s
输入会议文字(仅累积,不触发): [me:|我:|本机:] 前缀 = 本机说话人,[其他:|远程:] 或无前缀 = 匿名远端
命令: /decide 提交判断 | /show 查看 snapshot | /reset 清空 | /help | /quit"""


def parse_line(line: str) -> tuple[str, str | None]:
    """Split a speaker prefix; local prefixes map to 本机, remote ones stay anonymous."""
    stripped = line.strip()
    head, sep, rest = stripped.partition(':')
    if sep and rest.strip():
        label = head.strip()
        if label in LOCAL_SPEAKER_PREFIXES or label.lower() in LOCAL_SPEAKER_PREFIXES:
            return rest.strip(), LOCAL_SPEAKER
        if label in REMOTE_SPEAKER_PREFIXES or label.lower() in REMOTE_SPEAKER_PREFIXES:
            return rest.strip(), None
    return stripped, None


class HarnessRepl:
    def __init__(self, client: GatewayClient, session: MeetingSession, *,
                 input_func=input, output=print):
        self.client = client
        self.session = session
        self.input = input_func
        self.print = output
        self._last_hash: str | None = None
        self._last_attempt = 0

    def run(self) -> int:
        self.print(BANNER.format(version=__version__, url=self.client.url,
                                 backend=self.client.backend,
                                 deadline=int(self.client.deadline_seconds)))
        while True:
            try:
                line = self.input(f'harness[{self.session.pending_count}新]> ')
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith('/'):
                if self._command(line) == 'quit':
                    break
            else:
                self._feed(line)
        self.print('bye')
        return 0

    def _feed(self, line: str) -> None:
        text, speaker = parse_line(line)
        if not text:
            return
        key = self.session.add_caption(text, speaker)
        who = speaker or '匿名远端'
        self.print(f'+ {key} [{who}] 未判={self.session.pending_count}')

    def _command(self, line: str) -> str | None:
        parts = line.split()
        name = parts[0].lower()
        if name in ('/quit', '/exit'):
            return 'quit'
        if name == '/decide':
            self.decide()
        elif name == '/show':
            self.show()
        elif name == '/reset':
            self.session = MeetingSession()
            self.print('已清空会议')
        elif name in ('/help', '/?'):
            self.print(BANNER.format(version=__version__, url=self.client.url,
                                     backend=self.client.backend,
                                     deadline=int(self.client.deadline_seconds)))
        else:
            self.print(f'未知命令 {name}: /decide /show /reset /help /quit')
        return None

    def show(self) -> None:
        snapshot = self.session.snapshot()
        self.print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        self.print(f'generation={snapshot["generation"]} '
                   f'decided={self.session.decided_generation} '
                   f'未判={self.session.pending_count}')

    def decide(self) -> None:
        snapshot = self.session.snapshot()
        content_hash = self.client.content_hash(snapshot)
        if content_hash == self._last_hash:
            self._last_attempt += 1
        else:
            self._last_attempt = 0
        self._last_hash = content_hash
        attempt = self._last_attempt
        if self.session.pending_count == 0 and attempt == 0:
            self.print('⚠ 无新句: 真实 advisor 此刻不会发起任务(prepare_job 空转),强制提交')
        if attempt > 0:
            self.print(f'内容与上次相同 → 强制重判(request_id 后缀 -r{attempt})')
        fresh = snapshot['fresh_input_keys']
        started = time.monotonic()
        try:
            task = self.client.decide(snapshot, attempt=attempt)
        except GatewayError as exc:
            self.print(f'✗ gateway 失败 [{exc.code}] {exc}')
            return
        elapsed = time.monotonic() - started
        result = task.get('result') or {}
        timings = result.get('timings') or {}
        decision, reason = validate_decision(result.get('decision'))
        self.print(f'✓ 完成 elapsed={elapsed:.1f}s fresh={len(fresh)} '
                   f'timings={json.dumps(timings, ensure_ascii=False)}')
        self.print(json.dumps(result.get('decision'), ensure_ascii=False, indent=2))
        if decision is None:
            self.print(f'✗ decision 非法: {reason}')
            return
        self.print('校验: 合法 (decision-v1)')
        if decision['reply'] is not None:
            self.print(f'↩ reply 已记入 sent_replies: {decision["reply"][:80]}')
        self.session.apply_decision(decision)
