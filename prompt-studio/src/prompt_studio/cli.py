"""CLI: init prompts, replay a transcript, submit provider tokens."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__
from .clients import ClientError
from .doc_fetch import (DocFetchError, append_scene_document, fetch_doc,
                        write_text)
from .replay import (DEFAULT_PACED_SECONDS, ReplayError, ReplayRun,
                     validate_paced_seconds)
from .results import ReplayResult
from .stack import PROMPT_FILES, ROOT, Stack, StackConfig, StackError
from .transcript import Transcript, TranscriptError, parse_text

DEFAULT_MODEL = 'nio/DeepSeek-V4.1-Flash'
DEFAULT_OWNER = 'ou_prompt_studio'


class CommandError(RuntimeError):
    pass


def default_data_dir() -> Path:
    return ROOT / 'data' / 'prompt-studio'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='prompt-studio',
        description='把 admin-site 导出的会议文字记录重放进真实 advisor + gateway。')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    init = sub.add_parser('init', help='创建 data-dir 并从 llm-gateway 复制提示词')
    init.add_argument('--data-dir', type=Path, default=None)
    init.add_argument('--prompt-dir', type=Path, default=None)
    init.add_argument('--force', action='store_true',
                      help='覆盖已有的提示词文件')

    replay = sub.add_parser('replay', help='重放 TXT 或场景 JSON')
    source = replay.add_mutually_exclusive_group(required=True)
    source.add_argument('--transcript', type=Path,
                        help='admin-site 导出的 TXT；- 表示 stdin')
    source.add_argument('--scene', type=Path, help='场景 JSON')
    replay.add_argument('--title', default=None)
    replay.add_argument('--description', default=None)
    replay.add_argument('--document', type=Path, action='append', default=[],
                        help='附加文档（可重复，title 取文件名）')
    replay.add_argument('--meeting-no', default=None)
    replay.add_argument('--run-id', default=None)
    replay.add_argument('--owner', default=None)
    replay.add_argument('--mode', choices=['auto', 'paced'], default='auto')
    replay.add_argument('--model', default=None)
    replay.add_argument('--data-dir', type=Path, default=None)
    replay.add_argument('--prompt-dir', type=Path, default=None)
    replay.add_argument('--opencode-auth', type=Path, default=None)
    replay.add_argument('--opencode-provider-config', type=Path, default=None)
    replay.add_argument('--jira-url', default=None,
                        help='Jira Data Center HTTPS base URL；默认继承 JIRA_URL')
    replay.add_argument('--nsdp-token-file', type=Path, default=None)
    replay.add_argument('--jira-token-file', type=Path, default=None)
    replay.add_argument(
        '--paced-seconds', type=float, default=DEFAULT_PACED_SECONDS,
        help='paced 模式每行之间的等待秒数（默认 5；0 表示尽快）')
    replay.add_argument('--json', action='store_true', help='只输出结果 JSON')
    replay.add_argument('--debug', action='store_true')

    submit = sub.add_parser('submit-tokens',
                            help='向 auth stub 提交 NSDP/Jira token')
    submit.add_argument('--data-dir', type=Path, default=None)
    submit.add_argument('--owner', default=None)
    submit.add_argument('--nsdp-token-file', type=Path, default=None)
    submit.add_argument('--jira-token-file', type=Path, default=None)
    submit.add_argument('--debug', action='store_true')

    web = sub.add_parser('serve', help='启动本地 Web UI')
    web.add_argument(
        '--host', default='127.0.0.1',
        help='绑定地址：127.0.0.1（默认）或 0.0.0.0（可信内网）')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--data-dir', type=Path, default=None)
    web.add_argument('--prompt-dir', type=Path, default=None)
    web.add_argument('--owner', default=None)
    web.add_argument('--model', default=None)
    web.add_argument('--opencode-auth', type=Path, default=None)
    web.add_argument('--opencode-provider-config', type=Path, default=None)
    web.add_argument('--jira-url', default=None,
                     help='Jira Data Center HTTPS base URL；默认继承 JIRA_URL')
    web.add_argument('--nsdp-token-file', type=Path, default=None)
    web.add_argument('--jira-token-file', type=Path, default=None)
    web.add_argument('--debug', action='store_true')

    doc = sub.add_parser('doc', help='飞书文档工具')
    doc_sub = doc.add_subparsers(dest='doc_command', required=True)
    fetch = doc_sub.add_parser(
        'fetch', help='用个人 noam/my-lark 抓取飞书文档为 markdown')
    fetch.add_argument('--url', required=True, help='飞书 docx/wiki URL')
    fetch.add_argument('--title', default=None, help='覆盖文档标题')
    fetch.add_argument('--output', type=Path, default=None,
                       help='把正文写入文件（markdown）')
    fetch.add_argument('--scene', type=Path, default=None,
                       help='把文档追加到场景 JSON 的 documents[]')
    fetch.add_argument('--account', default=None, help='my-lark 账号名')
    fetch.add_argument('--noam', type=Path, default=None,
                       help='noam 可执行文件路径')
    fetch.add_argument('--noam-profile', default=None,
                       help='传给 noam 的 NOAM_PROFILE（如 dev）')
    fetch.add_argument('--timeout', type=float, default=90.0)
    fetch.add_argument('--json', action='store_true', help='输出文档 JSON')
    fetch.add_argument('--debug', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            return command_init(args)
        if args.command == 'replay':
            return command_replay(args)
        if args.command == 'submit-tokens':
            return command_submit_tokens(args)
        if args.command == 'serve':
            return command_serve(args)
        if args.command == 'doc':
            return command_doc_fetch(args)
        parser.error(f'unknown command: {args.command}')
    except KeyboardInterrupt:
        print('\nprompt-studio: interrupted', file=sys.stderr)
        return 130
    except (CommandError, ReplayError, StackError, ClientError, OSError) as exc:
        print(f'prompt-studio: {exc}', file=sys.stderr)
        if getattr(args, 'debug', False):
            raise
        return 1
    return 0


def command_init(args) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    prompt_dir = Path(args.prompt_dir) if args.prompt_dir else data_dir / 'prompts'
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
        prompt_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CommandError(f'cannot create data dir: {exc}') from None
    source = _gateway_prompts()
    copied, kept = [], []
    for name in PROMPT_FILES:
        src = source / name
        if not src.is_file():
            raise CommandError(f'gateway prompt missing: {src}')
        dst = prompt_dir / name
        if dst.exists() and not args.force:
            kept.append(name)
            continue
        shutil.copyfile(src, dst)
        copied.append(name)
    print(f'data-dir: {data_dir}')
    print(f'prompt-dir: {prompt_dir}')
    print('copied: ' + (', '.join(copied) or '(none)'))
    if kept:
        print('kept: ' + ', '.join(kept) + ' (use --force to overwrite)')
    print(f'next: prompt-studio replay --transcript <file> --data-dir {data_dir}')
    return 0


def command_submit_tokens(args) -> int:
    owner = _owner(args)
    nsdp = _read_token(args.nsdp_token_file, 'NSDP_TOKEN')
    jira = _read_token(args.jira_token_file, 'JIRA_TOKEN')
    if not nsdp and not jira:
        raise CommandError('没有 token：用 --nsdp-token-file/--jira-token-file '
                           '或 NSDP_TOKEN/JIRA_TOKEN 环境变量')
    data_dir = _data_dir(args)
    stack = Stack(StackConfig(data_dir=data_dir, owner=owner))
    try:
        stack.start(auth_only=True)
        stack.ensure_owner(owner, nsdp_token=nsdp, jira_token=jira)
    finally:
        stack.stop()
    providers = [name for name, value in (('nsdp', nsdp), ('jira', jira)) if value]
    print(f'owner: {owner}')
    print('submitted: ' + ', '.join(providers))
    print(f'data-dir: {data_dir}')
    return 0


def command_doc_fetch(args) -> int:
    if args.timeout <= 0:
        raise CommandError('--timeout 必须大于 0')
    try:
        doc = fetch_doc(args.url, noam=args.noam, account=args.account,
                        profile=args.noam_profile, timeout=args.timeout)
        title = doc.title if args.title is None else args.title.strip()
        if not title:
            raise CommandError('文档标题不能为空')
        scene_path = output_path = None
        if args.scene is not None:
            scene_path = append_scene_document(args.scene, title, doc.text)
        if args.output is not None:
            output_path = write_text(args.output, doc.text)
    except DocFetchError as exc:
        raise CommandError(str(exc)) from None
    payload = {'title': title, 'text': doc.text, 'url': doc.url}
    if scene_path is not None:
        payload['scene'] = str(scene_path)
    if output_path is not None:
        payload['output'] = str(output_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif scene_path is not None or output_path is not None:
        if scene_path is not None:
            print(f'added: {title} -> {scene_path}')
        if output_path is not None:
            print(f'wrote: {output_path}')
    else:
        sys.stdout.write(doc.text)
        if not doc.text.endswith('\n'):
            sys.stdout.write('\n')
    return 0


def command_serve(args) -> int:
    from .web_http import serve, validate_bind

    try:
        validate_bind(args.host)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if not 0 <= args.port <= 65535:
        raise CommandError('port 必须在 0-65535')

    data_dir = _data_dir(args)
    owner = _owner(args)
    model = args.model or os.environ.get('PROMPT_STUDIO_MODEL') or DEFAULT_MODEL
    prompt_dir = _ensure_web_prompts(data_dir, args.prompt_dir)
    nsdp = _read_token(args.nsdp_token_file, 'NSDP_TOKEN')
    jira = _read_token(args.jira_token_file, 'JIRA_TOKEN')
    config = StackConfig(
        root=ROOT, data_dir=data_dir, prompt_dir=prompt_dir,
        model=model, owner=owner, opencode_auth=args.opencode_auth,
        opencode_provider_config=args.opencode_provider_config,
        jira_url=args.jira_url)
    stack = Stack(config)
    try:
        stack.start()
        if nsdp or jira:
            stack.ensure_owner(owner, nsdp_token=nsdp, jira_token=jira)
        else:
            stack.ensure_owner(owner)
        serve(stack, args.host, args.port, prompt_dir, owner)
    finally:
        stack.stop()
    return 0


def _ensure_web_prompts(data_dir: Path, explicit: Path | None) -> Path:
    prompt_dir = Path(explicit) if explicit else data_dir / 'prompts'
    try:
        prompt_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(prompt_dir, 0o700)
    except OSError as exc:
        raise CommandError(f'无法创建 prompt dir: {exc}') from None
    missing = [name for name in PROMPT_FILES
               if not (prompt_dir / name).is_file()]
    if not missing:
        return prompt_dir
    if explicit is not None:
        raise CommandError(f'prompt dir 缺少文件: {missing}')
    source = _gateway_prompts()
    for name in missing:
        shutil.copyfile(source / name, prompt_dir / name)
        os.chmod(prompt_dir / name, 0o600)
    return prompt_dir


def command_replay(args) -> int:
    data_dir = _data_dir(args)
    owner = _owner(args)
    model = args.model or os.environ.get('PROMPT_STUDIO_MODEL') or DEFAULT_MODEL
    prompt_dir = _prompt_dir(data_dir, args.prompt_dir)
    transcript = _load_transcript(args)
    if args.meeting_no is not None:
        _validate_meeting_no(args.meeting_no)
    if args.run_id is not None:
        _validate_run_id(args.run_id)
    paced_seconds = None
    if args.mode == 'paced':
        try:
            paced_seconds = validate_paced_seconds(args.paced_seconds)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
    nsdp = _read_token(args.nsdp_token_file, 'NSDP_TOKEN')
    jira = _read_token(args.jira_token_file, 'JIRA_TOKEN')
    config = StackConfig(
        root=ROOT, data_dir=data_dir, prompt_dir=prompt_dir,
        model=model, owner=owner, opencode_auth=args.opencode_auth,
        opencode_provider_config=args.opencode_provider_config,
        jira_url=args.jira_url)
    stack = Stack(config)
    result: ReplayResult | None = None
    try:
        stack.start()
        if nsdp or jira:
            stack.ensure_owner(owner, nsdp_token=nsdp, jira_token=jira)
        run = ReplayRun(stack, transcript, mode=args.mode, owner=owner,
                        meeting_no=args.meeting_no, run_id=args.run_id)
        if args.mode == 'auto':
            result = run.run_auto()
        else:
            assert paced_seconds is not None
            if not args.json:
                print(f'paced: {len(transcript.human_turns)} 行，'
                      f'间隔 {paced_seconds:g}s')
            result = run.run_paced(paced_seconds)
    finally:
        stack.stop()
    if result is None:
        raise CommandError('replay produced no result')
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_summary(result)
    if result.session_state is not None and result.session_state.error:
        print(f'warning: session error: {result.session_state.error}',
              file=sys.stderr)
    if not result.complete_jobs:
        print('error: no completed decision; see advisor log', file=sys.stderr)
        return 1
    return 0



def _print_latest_decision(result: ReplayResult):
    jobs = result.complete_jobs or result.jobs
    if not jobs:
        print('  (无决策)')
        return
    job = jobs[-1]
    if job.decision is None:
        print(f'  decision: state={job.state}')
        return
    control = job.decision.get('control')
    reply = job.decision.get('reply')
    print(f'  decision: {control}')
    print(f'  reply: {reply if reply else "(无)"}')


def _print_summary(result: ReplayResult):
    print(f'run: {result.run_id}')
    print(f'session: {result.session_id} meeting: {result.meeting_no}')
    print(f'jobs: {len(result.jobs)} complete: {len(result.complete_jobs)}')
    print(f'replies: {len(result.replies)}')
    if result.replies:
        print('last reply: ' + result.replies[-1])
    if result.material is not None:
        print(f'material: {result.material.get("state")} '
              f'docs={len(result.material.get("documents") or [])}')
    if result.advisor_log:
        print(f'advisor log: {result.advisor_log}')


def _data_dir(args) -> Path:
    return Path(args.data_dir) if args.data_dir else default_data_dir()


def _owner(args) -> str:
    return (args.owner or os.environ.get('PROMPT_STUDIO_OWNER')
            or DEFAULT_OWNER)


def _prompt_dir(data_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    candidate = data_dir / 'prompts'
    if all((candidate / name).is_file() for name in PROMPT_FILES):
        return candidate
    print(f'note: {candidate} 未初始化，使用 gateway 内置提示词；'
          '先运行 `prompt-studio init`', file=sys.stderr)
    return None


def _gateway_prompts() -> Path:
    candidates: list[Path] = []
    try:
        spec = importlib.util.find_spec('llm_gateway.prompts')
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        candidates.append(Path(spec.origin).parent)
    candidates.append(ROOT / 'llm-gateway' / 'src' / 'llm_gateway' / 'prompts')
    for candidate in candidates:
        if all((candidate / name).is_file() for name in PROMPT_FILES):
            return candidate
    raise CommandError('找不到 llm-gateway prompts；先安装 llm-gateway')


def _load_transcript(args) -> Transcript:
    try:
        if args.scene is not None:
            raw = Path(args.scene).read_text(encoding='utf-8')
            transcript = Transcript.from_dict(json.loads(raw))
        else:
            path = Path(args.transcript)
            raw = (sys.stdin.read() if str(path) == '-'
                   else path.read_text(encoding='utf-8'))
            transcript = parse_text(raw)
    except (OSError, ValueError, TranscriptError) as exc:
        raise CommandError(f'读取场景失败: {exc}') from None
    if args.title is not None:
        transcript.title = args.title
    if args.description is not None:
        transcript.description = args.description
    for path in args.document or []:
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as exc:
            raise CommandError(f'读取文档失败: {path}: {exc}') from None
        transcript.documents.append({'kind': 'doc', 'title': path.stem,
                                     'text': text})
    return transcript


def _read_token(path: Path | None, env_name: str) -> str | None:
    if path is not None:
        try:
            value = Path(path).read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise CommandError(f'读取 token 文件失败: {path}: {exc}') from None
    else:
        value = os.environ.get(env_name, '').strip()
    return value or None


def _validate_meeting_no(value: str):
    if not value.isdigit() or not 6 <= len(value) <= 12:
        raise CommandError('meeting-no 必须是 6-12 位数字')


def _validate_run_id(value: str):
    allowed = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-'
    if (not value or len(value) > 80 or '..' in value
            or any(char not in allowed for char in value)):
        raise CommandError('run-id 只能包含字母、数字、点、下划线、短横线，'
                           '不能包含 ..')
