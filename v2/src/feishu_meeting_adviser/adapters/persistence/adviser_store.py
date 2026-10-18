"""Durable meeting context and model jobs, separate from source acknowledgements."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid5

from ...models import ChangeKind, MaterialState, digest
from .source_store import CapacityError, StorageError, caption_from_json, decode_mapping, encode
from .sqlite import SQLiteDatabase
from .adviser_schema import initialize_adviser_schema, require_current_adviser_schema
from .adviser_repositories import AdviserUnitOfWork
from ...snapshot import SnapshotBuilder, is_reply_evidence_caption
from ...decision import DecisionApplier


class AdviserStore:
    def __init__(self, path: Path, *, max_database_bytes=150_000_000, max_payload_bytes=100_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        database = SQLiteDatabase.open_current(
            self.path, max_database_bytes=max_database_bytes)
        self.db = database.connection
        try:
            require_current_adviser_schema(self.db)
        except BaseException:
            self.db.close()
            raise

    @classmethod
    def initialize(cls, path: Path, *, max_database_bytes=150_000_000) -> None:
        database = SQLiteDatabase.initialize(
            path, max_database_bytes=max_database_bytes)
        try:
            initialize_adviser_schema(database)
        finally:
            database.close()

    @classmethod
    def open_readonly(cls, path: Path, *, max_database_bytes=150_000_000, max_payload_bytes=100_000_000):
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance.max_payload_bytes = max_payload_bytes
        instance.db = SQLiteDatabase.open_readonly(
            instance.path, max_database_bytes=max_database_bytes).connection
        try:
            require_current_adviser_schema(instance.db)
        except BaseException:
            instance.db.close()
            raise
        return instance

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

    def _check_capacity(self):
        if self.payload_bytes() > self.max_payload_bytes:
            raise CapacityError('adviser content capacity exceeded; input preserved at source')

    def unit_of_work(self, *, fault=None):
        return AdviserUnitOfWork(self.db, capacity_check=self._check_capacity, fault=fault)

    def payload_bytes(self):
        return sum(self.db.execute(f'SELECT COALESCE(SUM(length(CAST({column} AS BLOB))),0) FROM {table}').fetchone()[0]
                   for table, column in (('inputs','body'), ('context','body'), ('jobs','snapshot'), ('jobs','result'),
                                         ('outbox','content'), ('outbox','evidence'), ('meeting_materials','body')))

    def meeting(self, mid):
        row = self.db.execute('SELECT * FROM meetings WHERE meeting_id=?', (mid,)).fetchone()
        return dict(row) if row else None

    def require_meeting(self, mid):
        meeting = self.meeting(mid)
        if meeting is None:
            raise StorageError('unknown adviser meeting')
        return meeting

    def ensure_meeting(self, mid):
        self.db.execute('INSERT OR IGNORE INTO meetings(meeting_id) VALUES (?)', (mid,))

    def ensure_session(self, session):
        mid = session['meeting_id']
        instance = session.get('join_instance') or digest([session.get('at'),session.get('joined_at'),session.get('bot_user_id')])
        with self.transaction():
            self.ensure_meeting(mid)
            old = self.require_meeting(mid)['join_instance']
            if old is not None and old != instance:
                self.db.execute('UPDATE outbox SET state=\'cancelled\' WHERE meeting_id=? AND kind=\'reply\' AND state=\'pending\'',(mid,))
                self.db.execute('UPDATE jobs SET state=\'abandoned\' WHERE meeting_id=? AND state IN (\'pending\',\'running\')',(mid,))
                self.db.execute('UPDATE meetings SET generation=generation+1,decided_generation=generation+1,'
                    'participation=\'active\',error=NULL,retry_at=0,cooldown_until=0 WHERE meeting_id=?',(mid,))
            self.db.execute('UPDATE meetings SET join_instance=? WHERE meeting_id=?',(instance,mid))
        return instance

    def ensure_introduction(self, session, text, *, now):
        instance = self.ensure_session(session)
        message_uuid = str(uuid5(NAMESPACE_URL,digest(['adviser-introduction-v1',session['meeting_id'],instance])))
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO outbox(uuid,meeting_id,kind,content,evidence,generation,created_ns,answer_uuid) VALUES (?,?,\'introduction\',?,\'{}\',0,?,?)',
                            (message_uuid,session['meeting_id'],text,int(now*1_000_000_000),message_uuid))
        return dict(self.db.execute('SELECT * FROM outbox WHERE uuid=?',(message_uuid,)).fetchone())

    def ensure_role(self, mid, role_version):
        with self.transaction():
            old = self.require_meeting(mid)['role_version']
            if old is None:
                previous = self.db.execute('SELECT role_version FROM jobs WHERE meeting_id=? ORDER BY generation DESC LIMIT 1',(mid,)).fetchone()
                old = previous['role_version'] if previous else None
            if old is not None and old != role_version:
                self.db.execute('UPDATE meetings SET generation=generation+1,retry_at=0,error=NULL WHERE meeting_id=?',(mid,))
            self.db.execute('UPDATE meetings SET role_version=? WHERE meeting_id=?',(role_version,mid))

    def set_materials(self, mid, snapshot, *, now):
        binding = snapshot.get('binding')
        meeting = self.require_meeting(mid)
        if binding and (binding.get('meeting_id') != mid or binding.get('join_instance') != meeting['join_instance']):
            raise StorageError('material snapshot meeting/session mismatch')
        version = snapshot.get('context_version')
        if snapshot['state'] not in {
                MaterialState.DISABLED, MaterialState.READY, MaterialState.PARTIAL, MaterialState.UNAVAILABLE}:
            raise StorageError('invalid material snapshot state')
        with self.transaction():
            if meeting['material_version'] != version:
                self.db.execute('UPDATE meetings SET generation=generation+1,error=NULL,retry_at=0 WHERE meeting_id=?',(mid,))
                for job in self.db.execute('SELECT job_id,snapshot FROM jobs WHERE meeting_id=? AND state IN (\'pending\',\'running\')',(mid,)).fetchall():
                    self.db.execute('UPDATE jobs SET state=\'abandoned\',snapshot=? WHERE job_id=?',
                        (encode({'snapshot_hash':digest(decode_mapping(job['snapshot']))}),job['job_id']))
            self.db.execute('UPDATE meetings SET material_version=?,material_state=? WHERE meeting_id=?',
                            (version,snapshot['state'],mid))
            self.db.execute('INSERT INTO meeting_materials(meeting_id,body,loaded_ns) VALUES (?,?,?) ON CONFLICT(meeting_id) '
                            'DO UPDATE SET body=excluded.body,loaded_ns=excluded.loaded_ns',
                            (mid,encode(snapshot),int(now*1_000_000_000)))

    def mark_material_loading(self, mid):
        with self.transaction():
            changed = self.db.execute(
                'UPDATE meetings SET material_state=? WHERE meeting_id=?',
                (MaterialState.LOADING, mid),
            ).rowcount
            if changed != 1:
                raise StorageError('unknown adviser meeting')

    def list_meeting_ids(self):
        return [row['meeting_id'] for row in self.db.execute('SELECT meeting_id FROM meetings')]

    def materials(self, mid):
        row = self.db.execute('SELECT body FROM meeting_materials WHERE meeting_id=?',(mid,)).fetchone()
        return decode_mapping(row['body']) if row else None

    def ingest(self, mid, deliveries, *, now=None, fault=None):
        inserted = 0
        with self.transaction():
            self.ensure_meeting(mid)
            generation = self.require_meeting(mid)['generation']
            for delivery in deliveries:
                body = delivery.caption or delivery.status
                if not body or body.meeting_id != mid:
                    raise StorageError('adviser input meeting mismatch')
                added = self.db.execute('INSERT OR IGNORE INTO inputs(delivery_id,meeting_id,sequence,body,created_ns) VALUES (?,?,?,?,?)',
                    (delivery.delivery_id, mid, delivery.sequence, encode(delivery.to_dict()),
                     int((time.time() if now is None else now)*1_000_000_000))).rowcount
                if not added:
                    continue
                generation += 1
                inserted += 1
                if delivery.kind in (ChangeKind.ADDED, ChangeKind.UPDATED):
                    caption = delivery.caption
                    self.db.execute('INSERT INTO context(meeting_id,sentence_key,body,suppressed,generation) VALUES (?,?,?,0,?) ON CONFLICT(meeting_id,sentence_key) '
                        'DO UPDATE SET body=excluded.body,suppressed=0,generation=excluded.generation',
                        (mid, caption.sentence_key, encode(asdict(caption)), generation))
                elif delivery.kind == ChangeKind.CONFLICT:
                    self.db.execute('UPDATE context SET suppressed=1,generation=? WHERE meeting_id=? AND sentence_key=?',
                                    (generation, mid, delivery.caption.sentence_key))
                elif delivery.status:
                    self.db.execute('UPDATE meetings SET source_state=?,source_status=? WHERE meeting_id=?',
                                    (delivery.status.state.value,encode(asdict(delivery.status)),mid))
            self.db.execute('UPDATE meetings SET generation=? WHERE meeting_id=?', (generation, mid))
            if fault:
                fault('before_commit')
        return inserted

    reply_fresh = staticmethod(is_reply_evidence_caption)

    def context_rows(self, mid, *, limit):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM context WHERE meeting_id=? ORDER BY generation DESC LIMIT ?', (mid, limit))]

    def pending_reply_row(self, mid):
        row = self.db.execute(
            '''SELECT * FROM outbox WHERE meeting_id=? AND kind='reply' AND state='pending'
               ORDER BY created_ns DESC, channel='meeting' DESC LIMIT 1''',
            (mid,),
        ).fetchone()
        return dict(row) if row else None

    def recent_reply_rows(self, mid, *, limit):
        return [dict(row) for row in self.db.execute(
            '''SELECT content,state FROM outbox WHERE meeting_id=?
               AND state IN ('sent','uncertain') GROUP BY answer_uuid
               ORDER BY created_ns DESC LIMIT ?''',
            (mid, limit),
        )]

    def context_count(self, mid):
        return self.db.execute(
            'SELECT count(*) FROM context WHERE meeting_id=?', (mid,)).fetchone()[0]

    def context_row(self, mid, sentence_key):
        row = self.db.execute(
            'SELECT body,suppressed FROM context WHERE meeting_id=? AND sentence_key=?',
            (mid, sentence_key),
        ).fetchone()
        return dict(row) if row else None

    def snapshot(self, mid, settings, *, now, joined_at, bot_id=None, role_characters=0):
        return SnapshotBuilder(self).build(
            mid,
            settings,
            now=now,
            joined_at=joined_at,
            bot_id=bot_id,
            role_characters=role_characters,
        )

    def prepare_job(self, mid, settings, role_version, *, now, joined_at, bot_id=None, role_characters=0):
        meeting = self.require_meeting(mid)
        if (not meeting or meeting['generation'] <= meeting['decided_generation'] or meeting['retry_at'] > now
                or meeting['material_state'] in ('unavailable','loading')):
            return None
        snapshot = self.snapshot(mid, settings, now=now, joined_at=joined_at, bot_id=bot_id,role_characters=role_characters)
        if not snapshot['fresh_input_keys'] and snapshot['pending_reply'] is None:
            with self.transaction():
                for old in self.db.execute('SELECT job_id,snapshot FROM jobs WHERE meeting_id=? AND state IN (\'pending\',\'running\')', (mid,)).fetchall():
                    self.db.execute('UPDATE jobs SET state=\'abandoned\',snapshot=? WHERE job_id=?',
                        (encode({'snapshot_hash': digest(decode_mapping(old['snapshot']))}), old['job_id']))
                self.db.execute('UPDATE meetings SET decided_generation=generation,error=NULL,retry_at=0 WHERE meeting_id=?', (mid,))
            return None
        job_id = digest([mid, snapshot['generation'], role_version])
        if meeting['material_version']:
            job_id = digest([job_id,meeting['material_version']])
        with self.transaction():
            for old in self.db.execute('SELECT job_id,snapshot FROM jobs WHERE meeting_id=? AND state=\'pending\' AND generation<?',(mid,snapshot['generation'])).fetchall():
                self.db.execute('UPDATE jobs SET state=\'abandoned\',snapshot=? WHERE job_id=?',
                    (encode({'snapshot_hash':digest(decode_mapping(old['snapshot']))}),old['job_id']))
            self.db.execute('INSERT OR IGNORE INTO jobs(job_id,meeting_id,generation,snapshot,role_version,created_ns,context_version) VALUES (?,?,?,?,?,?,?)',
                            (job_id, mid, snapshot['generation'], encode(snapshot), role_version, int(now*1_000_000_000),meeting['material_version']))
            # A failed call may be retried after captions or reply evidence have
            # expired. Keep the pending job's input aligned with the current
            # freshness window instead of replaying its original snapshot.
            self.db.execute('UPDATE jobs SET snapshot=? WHERE job_id=? AND state=\'pending\'',
                            (encode(snapshot), job_id))
        return dict(self.db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone())

    def finish_job(self, job, decision, *, now=None, settings=None, joined_at=0, bot_id=None,
                   notifications=None, fault=None):
        return DecisionApplier(self).apply(
            job,
            decision,
            now=now,
            settings=settings,
            joined_at=joined_at,
            bot_id=bot_id,
            notifications=notifications,
            fault=fault,
        )

    def fail_job(self, job, reason, *, retry_at):
        with self.unit_of_work() as tx:
            tx.jobs.retry(job['job_id'])
            tx.meetings.record_job_failure(job['meeting_id'], reason=reason, retry_at=retry_at)

    def mark_job_running(self, job_id):
        with self.unit_of_work() as tx:
            tx.jobs.claim(job_id)

    def protected_meetings(self):
        return {row['meeting_id'] for row in self.db.execute('SELECT meeting_id FROM meetings WHERE generation>decided_generation '
                'UNION SELECT meeting_id FROM outbox WHERE state IN (\'pending\',\'sending\',\'uncertain\',\'rejected\')')}

    def recover_sends(self):
        with self.unit_of_work() as tx:
            tx.outbox.recover_sending()
            tx.jobs.recover_running()

    def reconcile_destinations(self, mid, notifications):
        """A removed destination must never receive an old pending answer."""
        with self.transaction():
            if not notifications.meeting_comment:
                self.db.execute('''UPDATE outbox SET state='cancelled',error='destination_disabled'
                    WHERE meeting_id=? AND kind='reply' AND channel='meeting' AND state='pending' ''', (mid,))
            placeholders = ','.join('?' for _ in notifications.recipients)
            sql = '''UPDATE outbox SET state='cancelled',error='destination_disabled'
                WHERE meeting_id=? AND kind='reply' AND channel='im' AND state='pending' '''
            if placeholders:
                sql += f'AND recipient_id NOT IN ({placeholders})'
            self.db.execute(sql, (mid, *notifications.recipients))

    def next_reply(self, mid, *, now):
        meeting = self.require_meeting(mid)
        if (meeting['participation'] != 'active' or meeting['generation'] != meeting['decided_generation']
                or meeting['material_state'] in ('unavailable','loading')):
            return None
        row = self.db.execute('''SELECT * FROM outbox AS candidate
            WHERE meeting_id=? AND kind='reply' AND state='pending' AND generation=?
              AND (? >= ? OR EXISTS(
                  SELECT 1 FROM outbox AS prior WHERE prior.answer_uuid=candidate.answer_uuid
                  AND prior.state IN ('sent','uncertain','rejected')))
            ORDER BY created_ns,channel='meeting' DESC LIMIT 1''',
            (mid,meeting['generation'],now,meeting['cooldown_until'])).fetchone()
        return dict(row) if row and row['context_version']==meeting['material_version'] else None

    def discard_expired_replies(self, mid, settings, *, now, joined_at, bot_id=None):
        cancelled = 0
        with self.transaction():
            for row in self.db.execute('SELECT uuid,evidence FROM outbox WHERE meeting_id=? AND kind=\'reply\' AND state=\'pending\'',(mid,)).fetchall():
                valid = True
                for key,fingerprint in decode_mapping(row['evidence']).items():
                    current = self.db.execute('SELECT body,suppressed FROM context WHERE meeting_id=? AND sentence_key=?',(mid,key)).fetchone()
                    if not current or current['suppressed']:
                        valid = False
                        break
                    caption = caption_from_json(current['body'])
                    if caption.fingerprint != fingerprint or not self.reply_fresh(caption,now=now,joined_at=joined_at,settings=settings,bot_id=bot_id):
                        valid = False
                        break
                if not valid:
                    self.db.execute('UPDATE outbox SET state=\'cancelled\',error=\'reply_expired_or_revised\' WHERE uuid=?',(row['uuid'],))
                    cancelled += 1
        return cancelled

    def list_replies(self, mid, *, show_text=False):
        result = []
        for row in self.db.execute('SELECT uuid,kind,state,error,content,channel,recipient_id FROM outbox WHERE meeting_id=? ORDER BY created_ns DESC LIMIT 100',(mid,)):
            item = {key:row[key] for key in ('uuid','kind','state','error','channel')}
            if row['recipient_id']:
                item['recipient'] = '…' + row['recipient_id'][-6:] if len(row['recipient_id']) > 6 else '…'
            if show_text:
                item['text'] = row['content']
            result.append(item)
        return result

    def resolve_reply(self, mid, message_uuid, outcome, *, now):
        if outcome not in ('sent','retry','cancel'):
            raise StorageError('invalid reply resolution')
        with self.unit_of_work() as tx:
            row = tx.outbox.resolve(mid, message_uuid, outcome, now=now)
            if outcome=='retry' and row['kind']=='reply':
                # Recheck current discussion before any retry; keep the original UUID if unchanged.
                tx.meetings.schedule_reply_recheck(mid)

    def mark_sending(self, message_uuid):
        with self.unit_of_work() as tx:
            tx.outbox.claim_sending(message_uuid)

    def mark_sent(self, message_uuid, *, now, interval):
        with self.unit_of_work() as tx:
            row = tx.outbox.confirm_sent(message_uuid, now=now)
            if row['kind'] == 'reply':
                tx.meetings.set_cooldown(row['meeting_id'], now + interval)

    def mark_send_error(self, message_uuid, reason, *, uncertain):
        with self.unit_of_work() as tx:
            tx.outbox.mark_error(message_uuid, reason, uncertain=uncertain)

    def reply_statistics(self, mid):
        counts = {r['state']:r['n'] for r in self.db.execute('SELECT state,count(*) n FROM outbox WHERE meeting_id=? GROUP BY state',(mid,))}
        return {'pending_replies':counts.get('pending',0), 'uncertain_replies':counts.get('uncertain',0)+counts.get('sending',0),
                'rejected_replies':counts.get('rejected',0)}

    def status(self, mid, *, active=False, now=None):
        meeting = self.meeting(mid)
        if not meeting:
            return {'model_state':'not_started'}
        pending = max(0,meeting['generation']-meeting['decided_generation'])
        running = self.db.execute('SELECT 1 FROM jobs WHERE meeting_id=? AND state=\'running\' LIMIT 1',(mid,)).fetchone()
        now = time.time() if now is None else now
        state = 'running' if active and running else 'backoff' if pending and meeting['retry_at']>now else 'waiting' if pending else 'idle'
        return {'consumer_active':active,'participation':meeting['participation'],
                'model_state':state,'model_error':meeting['error'],'pending_model_inputs':pending,
                'material_state':meeting['material_state'] or 'disabled','material_version':meeting['material_version']} | self.reply_statistics(mid)

    def prune_history(self, cutoff_ns):
        removed = 0
        protected = self.protected_meetings()
        with self.transaction(check_capacity=False):
            for row in self.db.execute('SELECT meeting_id FROM meetings WHERE generation=decided_generation').fetchall():
                mid = row['meeting_id']
                if mid in protected:
                    continue
                for table in ('inputs','jobs'):
                    removed += self.db.execute(f'DELETE FROM {table} WHERE meeting_id=? AND created_ns<?',(mid,cutoff_ns)).rowcount
                removed += self.db.execute('DELETE FROM outbox WHERE meeting_id=? AND kind=\'reply\' AND created_ns<?',(mid,cutoff_ns)).rowcount
        return removed

    def forget_meeting(self, mid):
        with self.transaction(check_capacity=False):
            for table in ('inputs', 'context', 'jobs', 'outbox', 'meeting_materials', 'meetings'):
                self.db.execute(f'DELETE FROM {table} WHERE meeting_id=?', (mid,))
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.execute('VACUUM')
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
