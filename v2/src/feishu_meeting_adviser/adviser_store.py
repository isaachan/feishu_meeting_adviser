"""Durable meeting context and model jobs, separate from source acknowledgements."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time

from .models import ChangeKind, digest, epoch_ns
from .storage import CapacityError, StorageError, caption_from_json, connect_private, decode_mapping, encode


class AdviserStore:
    def __init__(self, path: Path, *, max_database_bytes=150_000_000, max_payload_bytes=100_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        self.db = connect_private(self.path, max_database_bytes=max_database_bytes)
        try:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
                    decided_generation INTEGER NOT NULL DEFAULT 0, participation TEXT NOT NULL DEFAULT 'active',
                    source_state TEXT, error TEXT, retry_at REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS inputs (
                    delivery_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    body TEXT NOT NULL, created_ns INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS context (
                    meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL,
                    suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
                    PRIMARY KEY (meeting_id,sentence_key));
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, role_version TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS input_meeting ON inputs(meeting_id,sequence);
                CREATE INDEX IF NOT EXISTS job_meeting ON jobs(meeting_id,generation);
            ''')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self, *, check_capacity=True):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            yield
            if check_capacity and self.payload_bytes() > self.max_payload_bytes:
                raise CapacityError('adviser content capacity exceeded; input preserved at source')
            self.db.execute('COMMIT')
        except (Exception, KeyboardInterrupt):
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def payload_bytes(self):
        return sum(self.db.execute(f'SELECT COALESCE(SUM(length(CAST({column} AS BLOB))),0) FROM {table}').fetchone()[0]
                   for table, column in (('inputs','body'), ('context','body'), ('jobs','snapshot'), ('jobs','result')))

    def meeting(self, mid):
        row = self.db.execute('SELECT * FROM meetings WHERE meeting_id=?', (mid,)).fetchone()
        return dict(row) if row else None

    def ensure_meeting(self, mid):
        self.db.execute('INSERT OR IGNORE INTO meetings(meeting_id) VALUES (?)', (mid,))

    def ingest(self, mid, deliveries, *, now=None, fault=None):
        inserted = 0
        with self.transaction():
            self.ensure_meeting(mid)
            generation = self.meeting(mid)['generation']
            for delivery in deliveries:
                body = delivery.caption or delivery.status
                if not body or body.meeting_id != mid:
                    raise StorageError('adviser input meeting mismatch')
                added = self.db.execute('INSERT OR IGNORE INTO inputs VALUES (?,?,?,?,?)',
                    (delivery.delivery_id, mid, delivery.sequence, encode(delivery.to_dict()),
                     int((time.time() if now is None else now)*1_000_000_000))).rowcount
                if not added:
                    continue
                generation += 1
                inserted += 1
                if delivery.kind in (ChangeKind.ADDED, ChangeKind.UPDATED):
                    caption = delivery.caption
                    self.db.execute('INSERT INTO context VALUES (?,?,?,0,?) ON CONFLICT(meeting_id,sentence_key) '
                        'DO UPDATE SET body=excluded.body,suppressed=0,generation=excluded.generation',
                        (mid, caption.sentence_key, encode(asdict(caption)), generation))
                elif delivery.kind == ChangeKind.CONFLICT:
                    self.db.execute('UPDATE context SET suppressed=1,generation=? WHERE meeting_id=? AND sentence_key=?',
                                    (generation, mid, delivery.caption.sentence_key))
                elif delivery.status:
                    self.db.execute('UPDATE meetings SET source_state=? WHERE meeting_id=?',
                                    (delivery.status.state.value, mid))
            self.db.execute('UPDATE meetings SET generation=? WHERE meeting_id=?', (generation, mid))
            if fault:
                fault('before_commit')
        return inserted

    @staticmethod
    def fresh(caption, *, now, joined_at, settings, bot_id=None):
        timestamp = caption.end_ns if caption.end_ns is not None else caption.event_ns
        if timestamp is None or caption.speaker_id == bot_id and bot_id is not None:
            return False
        seconds = timestamp / 1_000_000_000
        return joined_at <= seconds <= now + 5 and seconds >= now - settings.freshness_seconds

    def snapshot(self, mid, settings, *, now, joined_at, bot_id=None):
        meeting = self.meeting(mid)
        if not meeting:
            raise StorageError('unknown adviser meeting')
        rows = self.db.execute('SELECT * FROM context WHERE meeting_id=? ORDER BY generation DESC LIMIT 1000', (mid,)).fetchall()
        captions, fresh_keys, fingerprints = [], [], {}
        used = 0
        for row in rows:
            caption = caption_from_json(row['body'])
            item = asdict(caption) | {'conflicted': bool(row['suppressed'])}
            size = len(encode(item))
            if used + size > settings.context_characters:
                continue  # Keep whole sentences; never trigger from silently truncated text.
            used += size
            captions.append(item)
            if not row['suppressed']:
                fingerprints[caption.sentence_key] = caption.fingerprint
                if row['generation'] > meeting['decided_generation'] and self.fresh(
                        caption, now=now, joined_at=joined_at, settings=settings, bot_id=bot_id):
                    fresh_keys.append(caption.sentence_key)
        def order(item):
            stamp = epoch_ns(item['end_time'], numeric_milliseconds=True) or epoch_ns(item['event_time'])
            return (stamp is None, stamp or 0, item['sentence_key'])
        captions.sort(key=order)
        return {'generation': meeting['generation'], 'participation': meeting['participation'],
                'source_state': meeting['source_state'], 'captions': captions,
                'fresh_input_keys': fresh_keys, 'evidence': fingerprints,
                'context_omitted': len(captions) < self.db.execute('SELECT count(*) FROM context WHERE meeting_id=?', (mid,)).fetchone()[0],
                'pending_reply': None, 'sent_replies': []}

    def prepare_job(self, mid, settings, role_version, *, now, joined_at, bot_id=None):
        meeting = self.meeting(mid)
        if not meeting or meeting['generation'] <= meeting['decided_generation'] or meeting['retry_at'] > now:
            return None
        snapshot = self.snapshot(mid, settings, now=now, joined_at=joined_at, bot_id=bot_id)
        if not snapshot['fresh_input_keys'] and snapshot['pending_reply'] is None:
            with self.transaction():
                self.db.execute('UPDATE meetings SET decided_generation=generation,error=NULL,retry_at=0 WHERE meeting_id=?', (mid,))
            return None
        job_id = digest([mid, snapshot['generation'], role_version])
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO jobs(job_id,meeting_id,generation,snapshot,role_version,created_ns) VALUES (?,?,?,?,?,?)',
                            (job_id, mid, snapshot['generation'], encode(snapshot), role_version, int(now*1_000_000_000)))
        return dict(self.db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone())

    def finish_job(self, job, decision):
        with self.transaction():
            saved = self.db.execute('SELECT state FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            if not saved:
                raise StorageError('unknown adviser job')
            if saved['state'] == 'complete':
                return False
            self.db.execute('UPDATE jobs SET state=\'complete\',result=? WHERE job_id=?', (encode(decision.to_dict()), job['job_id']))
            self.db.execute('UPDATE meetings SET decided_generation=MAX(decided_generation,?),error=NULL,retry_at=0 WHERE meeting_id=?',
                            (job['generation'], job['meeting_id']))
        return True

    def fail_job(self, job, reason, *, retry_at):
        with self.transaction():
            self.db.execute('UPDATE meetings SET error=?,retry_at=? WHERE meeting_id=?', (reason, retry_at, job['meeting_id']))

    def protected_meetings(self):
        return {row['meeting_id'] for row in self.db.execute('SELECT meeting_id FROM meetings WHERE generation>decided_generation')}

    def forget_meeting(self, mid):
        with self.transaction(check_capacity=False):
            for table in ('inputs', 'context', 'jobs', 'meetings'):
                self.db.execute(f'DELETE FROM {table} WHERE meeting_id=?', (mid,))
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.execute('VACUUM')
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
