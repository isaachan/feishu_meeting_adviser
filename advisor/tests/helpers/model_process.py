"""Process fault fixture: local session, fake model transport, no external requests."""
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from feishu_meeting_adviser.adapters.persistence.factory import StorageFactory
from feishu_meeting_adviser.collector import utc_now
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.llm import CodexBackend
from feishu_meeting_adviser.models import Caption, SourceState, Status
from feishu_meeting_adviser.storage import Store

action, root = sys.argv[1], Path(sys.argv[2])
path = root / 'config.yaml'
if not path.exists():
    path.write_text('app:\n  id: synthetic\n  secret: synthetic\n'
                    'llm:\n  backend: codex\n  batch_seconds: 0\n  introduction: false\n  timeout_seconds: 30\n')
    path.chmod(0o600)

config = load_config(path)
StorageFactory(config).initialize_source()
StorageFactory(config).initialize_adviser()
source = Store(config.data_directory / 'source.sqlite3')
data = AdviserStore(config.data_directory / 'adviser.sqlite3')
session_file = root / 'session-id'
if session_file.exists():
    mid = session_file.read_text()
else:
    mid = data.create_local_session(now=time.time() - 30)['meeting_id']
    session_file.write_text(mid)
source.ensure_meeting(mid, {})
if source.caption(mid, 's:1') is None:
    stamp = utc_now()
    source.commit_page(mid, [Caption(mid, 's:1', 'SYNTHETIC MODEL QUESTION', stamp,
                                     event_time=stamp, speaker_id='synthetic-person')], {})
source.publish_status(Status(mid, SourceState.COLLECTING, 'scan_success', utc_now()))
source.close()
data.close()


def backend(settings, workdir, *, stop_event):
    peer = Path(__file__).with_name('fake_codex_server.py')
    return CodexBackend(settings, workdir, stop_event=stop_event,
        command=[sys.executable, str(peer), 'delay' if action == 'watch' else 'reply',
                 str(root / 'peer-pid')])


original_mark_sent = AdviserStore.mark_sent


def recording_mark_sent(self, message_uuid, *, now, interval):
    row = self.db.execute('SELECT content FROM outbox WHERE uuid=?', (message_uuid,)).fetchone()
    with (root / 'sent.jsonl').open('a') as stream:
        stream.write(json.dumps({'uuid': message_uuid, 'text': row['content']}) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    if action == 'crash-post':
        os._exit(99)  # Local publication recorded, confirmation not yet persisted.
    return original_mark_sent(self, message_uuid, now=now, interval=interval)


with patch('feishu_meeting_adviser.adviser.create_backend', side_effect=backend), \
     patch.object(AdviserStore, 'mark_sent', recording_mark_sent):
    args = ['--config', str(path), 'consume', mid]
    if action == 'watch':
        args.append('--watch')
    raise SystemExit(main(args))
