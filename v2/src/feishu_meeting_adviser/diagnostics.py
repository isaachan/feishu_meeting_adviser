"""Bounded rotating JSON diagnostics with an explicit non-content allowlist."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
from .storage import StorageError, private_directory
from .llm import MODEL_ERRORS

KINDS = {'page','error','health','maintenance','model','reply'}
TEXT_FIELDS = {
    'scan_mode': {'live','initial_replay','recovery_backfill'},
    'backend': {'codex','rule'},
    'participation': {'active','paused'},
    'state': {'collecting','recovering','incomplete','action_required','stopped','ended','not_started'},
    'error_kind': {'network','rate_limit','server','token_invalid','permission_or_admission_denied',
                   'bot_not_in_meeting','invalid_request','invalid_response','service_requires_manual_action',
                   'unknown_api_error','page_format','local_storage','source_worker_failed','diagnostic_record_limit'},
    'reason': {'scan_success','revision_conflict','recovery_completeness_unverified','restart_completeness_unverified',
               'resume_pending','backfill_in_progress','configuration_changed','configuration_unavailable','session_changed',
               'polling_stopped_bot_remains','not_started','bot_left_event','bot_membership_uncertain',
               'cursor_rejected_unverified','retained_history_trimmed','source_worker_failed','identity_degraded',
               'network','rate_limit','server','token_invalid','permission_or_admission_denied','bot_not_in_meeting',
               'invalid_request','invalid_response','service_requires_manual_action','unknown_api_error','page_format','local_storage'},
}
TEXT_FIELDS['error_kind'] |= MODEL_ERRORS | {'sender_interrupted','reply_expired_or_revised'}
TEXT_FIELDS['model_stage'] = {'startup','initialize','account_read','mcp_discovery','thread_start',
                             'mcp_verify','turn_start','generation','thread_cleanup'}
TEXT_FIELDS['reasoning_effort'] = {'none','minimal','low','medium','high','xhigh'}
NUMBER_FIELDS = {'event_count','unknown_times','identity_degraded','request_ms','processing_ms','query_start','query_end',
                 'http_status','code','retry_after','pending','content_bytes','database_bytes','risk_count',
                 'unresolved_conflicts','latest_caption_event_age_seconds','latest_caption_end_age_seconds',
                 'pruned_deliveries','expired_meetings'}
NUMBER_FIELDS |= {'model_ms','pending_model_inputs','pending_replies','uncertain_replies','rejected_replies'}
NUMBER_FIELDS |= {'model_stage_ms','mcp_inherited_servers','mcp_servers','mcp_catalog_tools',
                  'mcp_disabled_servers','mcp_unverified_servers','mcp_new_servers'}
BOOL_FIELDS = {'has_more','checkpoint_advanced','decision_saved','reply_confirmed'}
ENUM_COUNTERS = {'kinds': {'transcript_received','participant_joined','participant_left','chat_received','other'},
                 'counts': {'added','updated','conflict','duplicates','older'}}
LATENCIES = {'new_event_receive_seconds','new_end_receive_seconds'}


def numeric(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def sanitize(record):
    kind = record.get('kind')
    result = {'kind': kind if isinstance(kind,str) and kind in KINDS else 'error'}
    for key, allowed in TEXT_FIELDS.items():
        if isinstance(record.get(key),str) and record[key] in allowed:
            result[key] = record[key]
    for key in NUMBER_FIELDS:
        if numeric(record.get(key)):
            result[key] = record[key]
    for key in BOOL_FIELDS:
        if type(record.get(key)) is bool:
            result[key] = record[key]
    for key, allowed in ENUM_COUNTERS.items():
        if isinstance(record.get(key), dict):
            result[key] = {name:value for name,value in record[key].items() if name in allowed and type(value) is int and value >= 0}
    for key in LATENCIES:
        if isinstance(record.get(key), dict):
            result[key] = {name:value for name,value in record[key].items() if name in {'count','min','median','p95','max'} and numeric(value)}
    if isinstance(record.get('request_ids'), dict):
        result['request_id_hashes'] = {key:hashlib.sha256(value.encode()).hexdigest()[:16]
            for key,value in record['request_ids'].items()
            if key in {'request_id','log_id','x-request-id','x-tt-logid'} and isinstance(value,str)}
    return result


class Diagnostics:
    def __init__(self, directory: Path, *, max_bytes=1_000_000, backups=3):
        if type(max_bytes) is not int or max_bytes < 256 or type(backups) is not int or not 1 <= backups <= 20:
            raise ValueError('invalid diagnostics rotation bounds')
        self.directory = Path(directory)
        private_directory(self.directory)
        self.path = self.directory / 'diagnostics.jsonl'
        self.max_bytes = max_bytes
        self.backups = backups

    @contextmanager
    def _lock(self):
        path = self.directory / '.rotation.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os,'O_NOFOLLOW',0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _check_file(self, path):
        if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_mode & 0o077):
            raise StorageError('invalid diagnostics file permissions')

    def write(self, record: dict):
        from datetime import datetime, timezone
        safe = sanitize(record)
        safe['at'] = datetime.now(timezone.utc).isoformat()
        line = (json.dumps(safe, separators=(',',':'), ensure_ascii=False) + '\n').encode()
        if len(line) > self.max_bytes:
            line = (json.dumps({'at':safe['at'],'kind':'error','error_kind':'diagnostic_record_limit'})+'\n').encode()
        try:
            with self._lock():
                paths = [self.path] + [Path(str(self.path)+f'.{n}') for n in range(1,self.backups+1)]
                for path in self.directory.glob('diagnostics.jsonl.*'):
                    suffix = path.name.removeprefix('diagnostics.jsonl.')
                    if suffix.isdecimal() and int(suffix) > self.backups:
                        self._check_file(path)
                        path.unlink()
                for path in paths:
                    self._check_file(path)
                    if path.exists() and path.stat().st_size > self.max_bytes:
                        path.unlink()
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    paths[-1].unlink(missing_ok=True)
                    for index in range(self.backups,0,-1):
                        if paths[index-1].exists():
                            os.replace(paths[index-1], paths[index])
                fd = os.open(self.path, os.O_WRONLY|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0),0o600)
                with os.fdopen(fd,'ab') as out:
                    out.write(line)
                    out.flush()
                    os.fsync(out.fileno())
        except OSError:
            raise StorageError('diagnostics write failed; check space and private permissions') from None
