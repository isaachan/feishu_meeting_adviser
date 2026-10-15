"""Interactive local input for the production adviser decision loop."""
from argparse import ArgumentParser
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from uuid import uuid4

from feishu_meeting_adviser.adviser import AdviserService
from feishu_meeting_adviser.client import ApiError, StopRequested
from feishu_meeting_adviser.config import ConfigError, DEFAULT_CONFIG, load_config
from feishu_meeting_adviser.diagnostics import Diagnostics
from feishu_meeting_adviser.lifecycle import LifecycleError
from feishu_meeting_adviser.infrastructure.signals import stop_on_signals
from feishu_meeting_adviser.materials import MaterialError, load_materials
from feishu_meeting_adviser.oauth import OAuthError
from feishu_meeting_adviser.storage import StorageError, private_directory
from feishu_meeting_adviser.adapters.persistence.factory import StorageFactory
from .adapters import MaterialAdapter, SessionAdapter, TranscriptAdapter


def _read_text(path: Path, label: str, maximum: int = 1_000_000) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError()
        raw = path.read_bytes()
        if not 1 <= len(raw) <= maximum:
            raise OSError()
        value = raw.decode('utf-8')
        if not value.strip():
            raise OSError()
        return value
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f'{label} must be a non-empty local UTF-8 file') from None


class _HarnessOutput:
    """Keep the interactive terminal concise while retaining bounded full event records."""
    def __init__(self, directory: Path, *, stdout=sys.stdout, stderr=sys.stderr):
        self.stdout, self.stderr = stdout, stderr
        self.rotation = Diagnostics(Path(directory) / 'events',max_bytes=5_000_000,backups=3)
        self.rotation.path = self.rotation.directory / 'harness.jsonl'
        self.path = self.rotation.path
        self.last_error = None

    def __call__(self, line, flush=False):
        try:
            record = json.loads(line)
        except (TypeError,ValueError):
            record = {'kind':'harness_error','error':'invalid local harness event'}
            line = json.dumps(record,ensure_ascii=False)
        encoded = (line + '\n').encode()
        if len(encoded) > self.rotation.max_bytes:
            encoded = (json.dumps({'kind':'harness_error','error':'event exceeds local log limit'})+'\n').encode()
        self.rotation._append(encoded)
        kind = record.get('kind')
        if kind == 'reply' and record.get('status') == 'allowed':
            print('\n会参谋：' + record.get('text',''),file=self.stdout,flush=True)
        elif kind == 'context':
            print(json.dumps(record,ensure_ascii=False,indent=2),file=self.stdout,flush=True)
        elif kind in ('reload','reset','speaker'):
            print(json.dumps(record,ensure_ascii=False),file=self.stdout,flush=True)
        elif kind == 'harness_error':
            print('\nHarness error: ' + str(record.get('error','unknown error')),file=self.stderr,flush=True)
        elif kind == 'status' and record.get('model_error') and record.get('model_error') != self.last_error:
            self.last_error = record['model_error']
            print('\nModel error: ' + self.last_error,file=self.stderr,flush=True)


def _event_file(data_directory: Path) -> Path:
    return Path(data_directory) / 'events' / 'harness.jsonl'


def _open_event_file(path: Path):
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise StorageError('harness event log must be a private regular file')
    fd = os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    return os.fdopen(fd,'r',encoding='utf-8')


def _watch_events(path: Path, stop_event, write=print, *, poll_seconds=.2, initial_lines=10):
    """Follow the active event file and reopen it after bounded-log rotation."""
    stream = None
    first_open = True
    try:
        while not stop_event.is_set():
            if stream is None:
                if not path.exists():
                    stop_event.wait(poll_seconds)
                    continue
                stream = _open_event_file(path)
                lines = stream.readlines()
                for line in (lines[-initial_lines:] if first_open else lines):
                    write(line.rstrip('\n'))
                first_open = False
            line = stream.readline()
            if line:
                write(line.rstrip('\n'))
                continue
            try:
                current = path.stat()
                opened = os.fstat(stream.fileno())
                rotated = (current.st_dev,current.st_ino) != (opened.st_dev,opened.st_ino)
                truncated = current.st_size < stream.tell()
            except FileNotFoundError:
                rotated = True
                truncated = False
            if rotated:
                stream.close()
                stream = None
            elif truncated:
                stream.seek(0)
            stop_event.wait(poll_seconds)
    finally:
        if stream is not None:
            stream.close()


class Harness:
    def __init__(self, config_factory, description_file: Path, oauth_directory: Path, display=print):
        self.config_factory = config_factory
        self.description_file = description_file
        self.oauth_directory = oauth_directory
        self._write = display
        self._output_lock = threading.Lock()
        config = config_factory()
        self.shutdown_timeout = config.llm.timeout_seconds + 10
        self.root = config.data_directory
        private_directory(self.root)
        self.storage_factory = StorageFactory(config, root=self.root)
        self.transcript = TranscriptAdapter(self.storage_factory.source())
        self.speaker = '参会者 1'
        self.counter = 0
        self.runner = self.thread = self.stop_event = None
        self.last_context = self.last_status = None
        self.failure = None
        self.ready = threading.Event()
        self._state_lock = threading.Lock()
        try:
            self.start(new_meeting=True)
        except BaseException:
            self.transcript.close()
            raise

    def display(self, record):
        with self._output_lock:
            self._write(json.dumps(record, ensure_ascii=False), flush=True)

    def _session(self, mid):
        now = int(time.time())
        return {'phase':'joined', 'meeting_no':self.config_factory().meeting_no,
            'meeting_id':mid, 'bot_user_id':'harness-bot', 'joined_at':now,
            'join_instance':'harness-' + uuid4().hex, 'at':datetime.now(timezone.utc).isoformat()}

    def start(self, *, new_meeting, mid=None):
        if new_meeting:
            mid = 'harness-' + uuid4().hex
            self.session = self._session(mid)
        self.mid = mid
        self.transcript.start(mid)
        self.stop_event = threading.Event()
        self.submitted_since_start = 0
        self.ready.clear()
        self.failure = None
        self.thread = threading.Thread(target=self._run, name='meeting-adviser-harness', daemon=False)
        self.thread.start()
        if not self.ready.wait(10):
            raise LifecycleError('harness consumer did not start')
        if self.failure:
            raise self.failure

    def _run(self):
        source = runner = None
        try:
            config = self.config_factory()
            description = _read_text(self.description_file, 'description file')
            source = self.storage_factory.source()
            manager = SessionAdapter(source, config, self.session, self.display)
            client = MaterialAdapter(
                config, self.oauth_directory, description, self.session, self.stop_event)
            def material_loader(cfg, session, **kwargs):
                return load_materials(cfg,session,client=client,emit=kwargs.get('emit'),
                    stop_event=kwargs.get('stop_event'),clock=kwargs.get('clock',time.time))
            def save_context(snapshot):
                with self._state_lock:
                    self.last_context = snapshot
                self.display({'kind':'model_input', 'generation':snapshot.get('generation'),
                    'fresh_input_keys':snapshot.get('fresh_input_keys',[]),
                    'context_omitted':snapshot.get('context_omitted',False)})
            def save_decision(decision):
                self.display({'kind':'model_decision', **decision.to_dict()})
            runner = AdviserService(source,manager,stop_event=self.stop_event,
                material_client=client,material_loader=material_loader,preview_display=self.display,
                model_input_observer=save_context,decision_observer=save_decision,
                diagnose=lambda record:self.display({'kind':'diagnostic',**record}))
            self.runner = runner
            self.ready.set()
            runner.run(watch=True,notify=self._status)
        except BaseException as exc:
            self.failure = exc
            self.ready.set()
            self.display({'kind':'harness_error','error':str(exc)})
        finally:
            if runner:
                try:
                    runner.close()
                except BaseException as exc:
                    self.failure = self.failure or exc
                    self.display({'kind':'harness_error','error':str(exc)})
            if source:
                source.close()
            self.runner = None

    def _status(self, status):
        with self._state_lock:
            self.last_status = status
        interesting = {key:status.get(key) for key in ('model_state','model_error','participation','material_state')}
        self.display({'kind':'status',**interesting})

    def submit(self, speaker: str, text: str):
        if self.failure or not self.thread.is_alive():
            raise LifecycleError('harness consumer is not running; inspect harness_error')
        if not isinstance(speaker,str) or not 1 <= len(speaker.strip()) <= 80:
            raise ValueError('speaker name must contain 1 to 80 characters')
        if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8')) > 100_000:
            raise ValueError('caption text must contain 1 to 100000 UTF-8 bytes')
        speaker, text = speaker.strip(), text.strip()
        self.counter += 1
        self.submitted_since_start += 1
        try:
            key = self.transcript.submit(
                self.mid, self.counter, speaker, text)
        except BaseException:
            self.submitted_since_start -= 1
            raise
        self.display({'kind':'caption','speaker':speaker,'text':text,'sentence_key':key})

    def context(self):
        with self._state_lock:
            value = self.last_context
        self.display({'kind':'context','status':'not_available'} if value is None else {'kind':'context','snapshot':value})

    def restart(self, *, reset):
        mid = None if reset else self.mid
        self.stop()
        if reset:
            self.last_context = self.last_status = None
            self.counter = 0
        self.start(new_meeting=reset,mid=mid)
        self.display({'kind':'reset' if reset else 'reload','status':'ready'})

    def settled(self):
        with self._state_lock:
            status = dict(self.last_status or {})
        return bool(status) and status.get('offered',0) >= self.submitted_since_start and (status.get('model_error') or
            status.get('model_state') == 'idle' and status.get('pending_replies',0) == 0)

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=self.shutdown_timeout)
        if self.thread and self.thread.is_alive():
            raise LifecycleError('harness model worker did not stop')

    def close(self):
        try:
            self.stop()
        finally:
            self.transcript.close()


_SPEAKER = re.compile(r'^\s*([^:：]{1,80})\s*[:：]\s*(.+)$')


def _handle_line(harness: Harness, line: str):
    line = line.strip()
    if not line:
        return True
    if line == '/quit':
        return False
    if line == '/context':
        harness.context()
        return True
    if line == '/reload':
        harness.restart(reset=False)
        return True
    if line == '/reset':
        harness.restart(reset=True)
        return True
    if line.startswith('/speaker '):
        speaker = line[len('/speaker '):].strip()
        if not speaker or len(speaker) > 80:
            raise ValueError('speaker name must contain 1 to 80 characters')
        harness.speaker = speaker
        harness.display({'kind':'speaker','name':speaker})
        return True
    if line.startswith('/'):
        raise ValueError('unknown command')
    match = _SPEAKER.match(line)
    speaker, text = (match.group(1).strip(),match.group(2).strip()) if match else (harness.speaker,line)
    harness.submit(speaker,text)
    return True


def _parser():
    parser = ArgumentParser(description='Exercise the production adviser loop with local simulated captions.')
    parser.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    parser.add_argument('--description-file',type=Path,required=True)
    parser.add_argument('--role-file',type=Path,required=True)
    parser.add_argument('--backend',choices=('codex','opencode'))
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--script',type=Path,help='JSONL rows: speaker, text, optional delay_seconds')
    return parser


def _watch_parser():
    parser = ArgumentParser(description='Follow the complete harness JSON event stream.')
    parser.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    parser.add_argument('--data-dir',type=Path)
    return parser


def _watch_main(argv):
    args = _watch_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        data = args.data_dir.absolute() if args.data_dir else config.data_directory / 'harness'
        path = _event_file(data)
        print('Watching harness events: ' + str(path),file=sys.stderr)
        stop_event = threading.Event()
        with stop_on_signals(stop_event.set):
            _watch_events(path,stop_event)
        return 0
    except (ConfigError,StorageError,OSError,UnicodeError) as exc:
        print('Watch stopped: ' + str(exc),file=sys.stderr)
        return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'watch':
        return _watch_main(argv[1:])
    args = _parser().parse_args(argv)
    harness = None
    try:
        base = load_config(args.config)
        description_file, role_file = args.description_file.absolute(), args.role_file.absolute()
        _read_text(description_file,'description file')
        _read_text(role_file,'role file',100_000)
        data = (args.data_dir.absolute() if args.data_dir else base.data_directory / 'harness')
        def config_factory():
            current = load_config(args.config)
            llm = replace(current.llm,role_file=role_file,introduction=False,
                backend=args.backend or current.llm.backend)
            if llm.backend not in ('codex','opencode'):
                raise ConfigError('harness requires --backend codex or opencode')
            context = replace(current.meeting_context,calendar_id='harness',event_id='harness',identity='user')
            return replace(current,data_directory=data,llm=llm,meeting_context=context)
        output = _HarnessOutput(data)
        harness = Harness(config_factory,description_file,base.data_directory,display=output)
        print('Harness ready. Enter “speaker: text”, or use /speaker, /context, /reload, /reset, /quit.',file=sys.stderr)
        print('Full event log: ' + str(output.path),file=sys.stderr)
        if args.script:
            script = _read_text(args.script.absolute(),'script file')
            for number,raw in enumerate(script.splitlines(),1):
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if not isinstance(row,dict) or not isinstance(row.get('speaker'),str) or not isinstance(row.get('text'),str):
                    raise ValueError(f'invalid script row {number}')
                delay = row.get('delay_seconds',0)
                if isinstance(delay,bool) or not isinstance(delay,(int,float)) or not 0 <= delay <= 3600:
                    raise ValueError(f'invalid script delay at row {number}')
                if harness.stop_event.wait(delay):
                    break
                harness.submit(row['speaker'],row['text'])
            deadline = time.monotonic()+config_factory().llm.timeout_seconds+config_factory().llm.batch_seconds+10
            while not harness.settled() and time.monotonic()<deadline and not harness.failure:
                time.sleep(0.1)
            return 1 if harness.failure or not harness.settled() else 0
        def stop_input():
            harness.stop_event.set()
            raise KeyboardInterrupt()
        with stop_on_signals(stop_input):
            def lines():
                if not sys.stdin.isatty():
                    yield from sys.stdin
                    return
                while True:
                    try:
                        yield input('发言> ')
                    except EOFError:
                        return
            for line in lines():
                try:
                    if not _handle_line(harness,line):
                        break
                except ValueError as exc:
                    print('Input rejected: ' + str(exc),file=sys.stderr)
                if harness.stop_event.is_set():
                    break
        return 1 if harness.failure else 0
    except (ConfigError,StorageError,LifecycleError,MaterialError,OAuthError,ApiError,OSError,ValueError,json.JSONDecodeError) as exc:
        print('Harness stopped: ' + str(exc),file=sys.stderr)
        return 1
    except (KeyboardInterrupt,StopRequested):
        return 130
    finally:
        if harness:
            harness.close()


if __name__ == '__main__':
    raise SystemExit(main())
