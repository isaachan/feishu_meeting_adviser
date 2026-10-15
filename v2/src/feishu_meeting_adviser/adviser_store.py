"""Durable meeting context and model jobs, separate from source acknowledgements."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid5

from .models import ChangeKind, digest, epoch_ns
from .llm import ModelError
from .storage import CapacityError, StorageError, caption_from_json, connect_private, decode_mapping, encode
from .adapters.persistence.sqlite import SQLiteDatabase
from .adapters.persistence.adviser_migrations import migrate_adviser, require_current_adviser_schema


class AdviserStore:
    def __init__(self, path: Path, *, max_database_bytes=150_000_000, max_payload_bytes=100_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        self.db = connect_private(self.path, max_database_bytes=max_database_bytes)
        try:
            migrate_adviser(self.path, self.db, max_database_bytes)
        except BaseException:
            self.db.close()
            raise

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

    def payload_bytes(self):
        return sum(self.db.execute(f'SELECT COALESCE(SUM(length(CAST({column} AS BLOB))),0) FROM {table}').fetchone()[0]
                   for table, column in (('inputs','body'), ('context','body'), ('jobs','snapshot'), ('jobs','result'),
                                         ('outbox','content'), ('outbox','evidence'), ('meeting_materials','body')))

    def meeting(self, mid):
        row = self.db.execute('SELECT * FROM meetings WHERE meeting_id=?', (mid,)).fetchone()
        return dict(row) if row else None

    def ensure_meeting(self, mid):
        self.db.execute('INSERT OR IGNORE INTO meetings(meeting_id) VALUES (?)', (mid,))

    def ensure_session(self, session):
        mid = session['meeting_id']
        instance = session.get('join_instance') or digest([session.get('at'),session.get('joined_at'),session.get('bot_user_id')])
        with self.transaction():
            self.ensure_meeting(mid)
            old = self.meeting(mid)['join_instance']
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
            self.db.execute('INSERT OR IGNORE INTO outbox(uuid,meeting_id,kind,content,evidence,generation,created_ns) VALUES (?,?,\'introduction\',?,\'{}\',0,?)',
                            (message_uuid,session['meeting_id'],text,int(now*1_000_000_000)))
        return dict(self.db.execute('SELECT * FROM outbox WHERE uuid=?',(message_uuid,)).fetchone())

    def ensure_role(self, mid, role_version):
        with self.transaction():
            old = self.meeting(mid)['role_version']
            if old is None:
                previous = self.db.execute('SELECT role_version FROM jobs WHERE meeting_id=? ORDER BY generation DESC LIMIT 1',(mid,)).fetchone()
                old = previous['role_version'] if previous else None
            if old is not None and old != role_version:
                self.db.execute('UPDATE meetings SET generation=generation+1,retry_at=0,error=NULL WHERE meeting_id=?',(mid,))
            self.db.execute('UPDATE meetings SET role_version=? WHERE meeting_id=?',(role_version,mid))

    def set_materials(self, mid, snapshot, *, now):
        binding = snapshot.get('binding')
        meeting = self.meeting(mid)
        if binding and (binding.get('meeting_id') != mid or binding.get('join_instance') != meeting['join_instance']):
            raise StorageError('material snapshot meeting/session mismatch')
        version = snapshot.get('context_version')
        if snapshot['state'] not in ('disabled','ready','partial','unavailable'):
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

    def materials(self, mid):
        row = self.db.execute('SELECT body FROM meeting_materials WHERE meeting_id=?',(mid,)).fetchone()
        return decode_mapping(row['body']) if row else None

    def ingest(self, mid, deliveries, *, now=None, fault=None):
        inserted = 0
        with self.transaction():
            self.ensure_meeting(mid)
            generation = self.meeting(mid)['generation']
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

    @staticmethod
    def fresh(caption, *, now, joined_at, settings, bot_id=None):
        timestamp = caption.end_ns if caption.end_ns is not None else caption.event_ns
        if timestamp is None or caption.speaker_id == bot_id and bot_id is not None:
            return False
        seconds = timestamp / 1_000_000_000
        return joined_at <= seconds <= now + 5 and seconds >= now - settings.freshness_seconds

    def snapshot(self, mid, settings, *, now, joined_at, bot_id=None, role_characters=0):
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
        pending = self.db.execute('SELECT * FROM outbox WHERE meeting_id=? AND kind=\'reply\' AND state=\'pending\' ORDER BY created_ns DESC LIMIT 1', (mid,)).fetchone()
        pending_reply = None
        reply_keys = list(fresh_keys)
        if pending:
            basis = decode_mapping(pending['evidence'])
            pending_reply = {'uuid': pending['uuid'], 'text': pending['content'], 'reply_sentence_keys': list(basis)}
            for key, fingerprint in basis.items():
                row = next((item for item in captions if item['sentence_key']==key), None)
                if row and fingerprints.get(key)==fingerprint:
                    caption = caption_from_json(encode({k:v for k,v in row.items() if k!='conflicted'}))
                    if self.fresh(caption, now=now, joined_at=joined_at, settings=settings, bot_id=bot_id):
                        reply_keys.append(key)
        sent = [{'text':r['content'], 'state':r['state']} for r in self.db.execute(
            'SELECT content,state FROM outbox WHERE meeting_id=? AND state IN (\'sent\',\'uncertain\') ORDER BY created_ns DESC LIMIT 20', (mid,))]
        result = {'generation': meeting['generation'], 'participation': meeting['participation'],
                'source_state': meeting['source_state'], 'captions': captions,
                'source_status': decode_mapping(meeting['source_status']) if meeting['source_status'] else None,
                'fresh_input_keys': fresh_keys, 'evidence': fingerprints,
                'context_omitted': len(captions) < self.db.execute('SELECT count(*) FROM context WHERE meeting_id=?', (mid,)).fetchone()[0],
                'pending_reply': pending_reply, 'sent_replies': sent, 'reply_allowed_keys': list(set(reply_keys))}

        material = self.materials(mid)
        if material and material['state'] != 'disabled':
            result['meeting_context'] = material
        # Count the entire serialized snapshot plus role, including evidence and prior replies.
        # Prefer captions to old sent messages and material text; keep source metadata.
        while len(encode(result)) + role_characters > settings.context_characters:
            result['context_omitted'] = True
            if result['sent_replies']:
                result['sent_replies'].pop()
            elif result.get('meeting_context') and any(item.get('text') for item in result['meeting_context']['documents']):
                item = max(result['meeting_context']['documents'],key=lambda value:len(value.get('text','')))
                excess = len(encode(result)) + role_characters - settings.context_characters
                item['text'] = item['text'][:-max(1,excess)]
                item['truncated'] = True
                result['meeting_context']['state'] = 'partial'
            elif result.get('meeting_context',{}).get('description'):
                context = result['meeting_context']
                excess = len(encode(result)) + role_characters - settings.context_characters
                context['description'] = context['description'][:-max(1,excess)]
                context['description_truncated'] = True
                context['state'] = 'partial'
            elif result['captions']:
                key = result['captions'].pop(0)['sentence_key']
                result['evidence'].pop(key,None)
                result['fresh_input_keys'] = [value for value in result['fresh_input_keys'] if value!=key]
                result['reply_allowed_keys'] = [value for value in result['reply_allowed_keys'] if value!=key]
            else:
                raise ModelError('model_context_budget')
        return result

    def prepare_job(self, mid, settings, role_version, *, now, joined_at, bot_id=None, role_characters=0):
        meeting = self.meeting(mid)
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
        return dict(self.db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone())

    def finish_job(self, job, decision, *, now=None, settings=None, joined_at=0, bot_id=None):
        now = time.time() if now is None else now
        snapshot = decode_mapping(job['snapshot'])
        fresh_keys = set(snapshot['fresh_input_keys'])
        reply_keys = set(snapshot.get('reply_allowed_keys', snapshot['fresh_input_keys']))
        if not set(decision.control_sentence_keys) <= fresh_keys or not set(decision.reply_sentence_keys) <= reply_keys:
            raise ModelError('model_invalid_evidence')
        # Check evidence against accepted current captions; do not act on revised/expired inputs.
        valid = {}
        for key in set(decision.control_sentence_keys) | set(decision.reply_sentence_keys):
            row = self.db.execute('SELECT body,suppressed FROM context WHERE meeting_id=? AND sentence_key=?', (job['meeting_id'],key)).fetchone()
            if row and not row['suppressed']:
                caption = caption_from_json(row['body'])
                if caption.fingerprint == snapshot['evidence'].get(key) and (settings is None or self.fresh(
                        caption, now=now, joined_at=joined_at, settings=settings, bot_id=bot_id)):
                    valid[key] = caption.fingerprint
        control_valid = all(key in valid for key in decision.control_sentence_keys)
        reply_valid = decision.reply is not None and all(key in valid for key in decision.reply_sentence_keys)
        with self.transaction():
            saved = self.db.execute('SELECT state FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            if not saved:
                raise StorageError('unknown adviser job')
            if saved['state'] in ('complete','abandoned'):
                return False
            meeting = self.meeting(job['meeting_id'])
            if job.get('context_version') != meeting['material_version'] or meeting['material_state']=='unavailable':
                self.db.execute('UPDATE jobs SET state=\'abandoned\' WHERE job_id=?',(job['job_id'],))
                return False
            participation = meeting['participation']
            if control_valid:
                if decision.control == 'pause':
                    participation = 'paused'
                elif decision.control == 'resume':
                    participation = 'active'
            self.db.execute('UPDATE outbox SET state=\'cancelled\' WHERE meeting_id=? AND kind=\'reply\' AND state=\'pending\'', (job['meeting_id'],))
            if reply_valid and participation == 'active' and not (control_valid and decision.control in ('pause','cancel')):
                basis = {key:valid[key] for key in decision.reply_sentence_keys}
                message_uuid = str(uuid5(NAMESPACE_URL, digest(['adviser-reply-v1',job['meeting_id'],job['role_version'],basis,decision.reply])))
                # An unchanged candidate keeps its UUID across a latest-context recheck.
                self.db.execute('INSERT INTO outbox(uuid,meeting_id,kind,content,evidence,generation,created_ns,context_version) VALUES (?,?,\'reply\',?,?,?,?,?) '
                    'ON CONFLICT(uuid) DO UPDATE SET generation=excluded.generation,context_version=excluded.context_version,'
                    'state=CASE WHEN outbox.state=\'cancelled\' THEN \'pending\' ELSE outbox.state END',
                    (message_uuid,job['meeting_id'],decision.reply,encode(basis),job['generation'],int(now*1_000_000_000),meeting['material_version']))
            brief = {'generation':job['generation'],'snapshot_hash':digest(snapshot),'context_version':meeting['material_version'],
                     'evidence':{key:snapshot['evidence'].get(key) for key in set(decision.control_sentence_keys)|set(decision.reply_sentence_keys)}}
            self.db.execute('UPDATE jobs SET state=\'complete\',result=?,snapshot=? WHERE job_id=?',
                            (encode(decision.to_dict()),encode(brief),job['job_id']))
            self.db.execute('UPDATE meetings SET decided_generation=MAX(decided_generation,?),participation=?,error=NULL,retry_at=0 WHERE meeting_id=?',
                            (job['generation'], participation, job['meeting_id']))
        return True

    def fail_job(self, job, reason, *, retry_at):
        with self.transaction():
            self.db.execute('UPDATE jobs SET state=\'pending\' WHERE job_id=?',(job['job_id'],))
            self.db.execute('UPDATE meetings SET error=?,retry_at=? WHERE meeting_id=?', (reason, retry_at, job['meeting_id']))

    def mark_job_running(self, job_id):
        with self.transaction():
            self.db.execute('UPDATE jobs SET state=\'running\' WHERE job_id=? AND state=\'pending\'',(job_id,))

    def protected_meetings(self):
        return {row['meeting_id'] for row in self.db.execute('SELECT meeting_id FROM meetings WHERE generation>decided_generation '
                'UNION SELECT meeting_id FROM outbox WHERE state IN (\'pending\',\'sending\',\'uncertain\',\'rejected\')')}

    def recover_sends(self):
        with self.transaction():
            self.db.execute('UPDATE outbox SET state=\'uncertain\',error=\'sender_interrupted\' WHERE state=\'sending\'')
            self.db.execute('UPDATE jobs SET state=\'pending\' WHERE state=\'running\'')

    def next_reply(self, mid, *, now):
        meeting = self.meeting(mid)
        if (meeting['participation'] != 'active' or meeting['generation'] != meeting['decided_generation']
                or meeting['cooldown_until'] > now or meeting['material_state'] in ('unavailable','loading')):
            return None
        row = self.db.execute('SELECT * FROM outbox WHERE meeting_id=? AND kind=\'reply\' AND state=\'pending\' AND generation=? ORDER BY created_ns LIMIT 1', (mid,meeting['generation'])).fetchone()
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
                    if caption.fingerprint != fingerprint or not self.fresh(caption,now=now,joined_at=joined_at,settings=settings,bot_id=bot_id):
                        valid = False
                        break
                if not valid:
                    self.db.execute('UPDATE outbox SET state=\'cancelled\',error=\'reply_expired_or_revised\' WHERE uuid=?',(row['uuid'],))
                    cancelled += 1
        return cancelled

    def list_replies(self, mid, *, show_text=False):
        result = []
        for row in self.db.execute('SELECT uuid,kind,state,error,content FROM outbox WHERE meeting_id=? ORDER BY created_ns DESC LIMIT 100',(mid,)):
            item = {key:row[key] for key in ('uuid','kind','state','error')}
            if show_text:
                item['text'] = row['content']
            result.append(item)
        return result

    def resolve_reply(self, mid, message_uuid, outcome, *, now):
        if outcome not in ('sent','retry','cancel'):
            raise StorageError('invalid reply resolution')
        with self.transaction():
            row = self.db.execute('SELECT * FROM outbox WHERE meeting_id=? AND uuid=?',(mid,message_uuid)).fetchone()
            if not row or row['state'] not in ('uncertain','sending','rejected'):
                raise StorageError('reply is not an unresolved send; check replies first')
            state = {'sent':'sent','retry':'pending','cancel':'cancelled'}[outcome]
            self.db.execute('UPDATE outbox SET state=?,error=NULL,sent_at=? WHERE uuid=?',
                            (state,now if outcome=='sent' else None,message_uuid))
            if outcome=='retry' and row['kind']=='reply':
                # Recheck current discussion before any retry; keep the original UUID if unchanged.
                self.db.execute('UPDATE meetings SET generation=generation+1,retry_at=0 WHERE meeting_id=?',(mid,))

    def mark_sending(self, message_uuid):
        with self.transaction():
            changed = self.db.execute('UPDATE outbox SET state=\'sending\' WHERE uuid=? AND state=\'pending\'', (message_uuid,)).rowcount
            if not changed:
                raise StorageError('reply is no longer pending')

    def mark_sent(self, message_uuid, *, now, interval):
        with self.transaction():
            row = self.db.execute('SELECT * FROM outbox WHERE uuid=?', (message_uuid,)).fetchone()
            if not row or row['state'] != 'sending':
                raise StorageError('reply send state mismatch')
            self.db.execute('UPDATE outbox SET state=\'sent\',sent_at=?,error=NULL WHERE uuid=?', (now,message_uuid))
            if row['kind'] == 'reply':
                self.db.execute('UPDATE meetings SET cooldown_until=? WHERE meeting_id=?', (now+interval,row['meeting_id']))

    def mark_send_error(self, message_uuid, reason, *, uncertain):
        with self.transaction():
            self.db.execute('UPDATE outbox SET state=?,error=? WHERE uuid=?', ('uncertain' if uncertain else 'rejected',reason,message_uuid))

    def reply_statistics(self, mid):
        counts = {r['state']:r['n'] for r in self.db.execute('SELECT state,count(*) n FROM outbox WHERE meeting_id=? GROUP BY state',(mid,))}
        return {'pending_replies':counts.get('pending',0), 'uncertain_replies':counts.get('uncertain',0)+counts.get('sending',0),
                'rejected_replies':counts.get('rejected',0)}

    def status(self, mid, *, active=False, now=None):
        meeting = self.meeting(mid)
        if not meeting:
            return {'backend':'codex','model_state':'not_started'}
        pending = max(0,meeting['generation']-meeting['decided_generation'])
        running = self.db.execute('SELECT 1 FROM jobs WHERE meeting_id=? AND state=\'running\' LIMIT 1',(mid,)).fetchone()
        now = time.time() if now is None else now
        state = 'running' if active and running else 'backoff' if pending and meeting['retry_at']>now else 'waiting' if pending else 'idle'
        return {'backend':'codex','consumer_active':active,'participation':meeting['participation'],
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
