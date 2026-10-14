"""Local-only configuration. Legacy credential fields are readable, never rewritten."""
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import yaml
from .client import Credentials
from .collector import ScanSettings
from .llm import LLMSettings

DEFAULT_CONFIG = Path('config.yaml')


class ConfigError(Exception):
    pass


@dataclass(frozen=True, repr=False)
class Configuration:
    path: Path
    fingerprint: str
    credentials: Credentials
    meeting_no: str
    password: str
    data_directory: Path
    scan: ScanSettings = field(default_factory=ScanSettings)
    timeout_seconds: float = 10
    request_seconds: float = 0.7
    poll_seconds: float = 0.7
    cooldown_seconds: float = 30
    content_capacity_bytes: int = 100_000_000
    retention_days: int = 7
    diagnostic_bytes: int = 1_000_000
    diagnostic_backups: int = 3
    database_capacity_bytes: int = 150_000_000
    llm: LLMSettings = field(default_factory=LLMSettings)


def file_fingerprint(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ConfigError('configuration must be a regular local file with mode 600')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path: Path = DEFAULT_CONFIG) -> Configuration:
    path = Path(path).absolute()
    try:
        fingerprint = file_fingerprint(path)
        raw = yaml.safe_load(path.read_text())
    except (OSError, ValueError, yaml.YAMLError):
        raise ConfigError('unable to read local YAML configuration; content hidden') from None
    if not isinstance(raw, dict):
        raise ConfigError('configuration must be a mapping')
    def section(name):
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f'{name} must be a mapping')
        return value
    def string(values, key, default='', required=False):
        value = values.get(key, default)
        if not isinstance(value, str) or (required and not value.strip()):
            raise ConfigError(f'invalid configuration field: {key}')
        return value
    def number(values, key, default, low, high, integer=False):
        value = values.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not low <= value <= high or integer and type(value) is not int):
            raise ConfigError(f'invalid numeric configuration field: {key}')
        return value
    app = section('app')
    credentials = Credentials(string(app, 'id', required=True), string(app, 'secret', required=True))
    meeting = section('meeting')
    legacy_lookup = section('lookup')
    meeting_no = string(meeting, 'meeting_no', meeting.get('no', meeting.get(False, string(legacy_lookup, 'meeting_no'))))
    if len(meeting_no) != 9 or not meeting_no.isascii() or not meeting_no.isdecimal():
        raise ConfigError('meeting.meeting_no (or legacy lookup.meeting_no) must be a quoted nine-digit string')
    password = string(meeting, 'password', string(section('join'), 'password'))
    collector = section('collector')
    unknown = set(collector) - set(ScanSettings.__dataclass_fields__)
    if unknown:
        raise ConfigError('unknown collector configuration fields')
    try:
        scan = ScanSettings(**collector)
    except (ValueError, TypeError):
        raise ConfigError('invalid collector configuration') from None
    storage = section('storage')
    directory = Path(string(storage, 'directory', 'data'))
    if not directory.is_absolute():
        directory = path.parent / directory
    http = section('http')
    runtime = section('runtime')
    legacy_probe = section('probe')
    timeout = number(http, 'timeout_seconds', number(legacy_probe, 'timeout_seconds', 10, 1, 60), 1, 60)
    llm = section('llm')
    if set(llm) - set(LLMSettings.__dataclass_fields__):
        raise ConfigError('unknown llm configuration fields')
    backend = string(llm, 'backend', 'rule')
    if backend not in ('rule', 'codex'):
        raise ConfigError('llm.backend must be rule or codex')
    role_file = string(llm, 'role_file')
    role_path = Path(role_file) if role_file else None
    if role_path and not role_path.is_absolute():
        role_path = path.parent / role_path
    introduction = llm.get('introduction', True)
    if type(introduction) is not bool:
        raise ConfigError('llm.introduction must be boolean')
    llm_settings = LLMSettings(backend=backend, executable=string(llm, 'executable', 'codex', required=True),
        model=string(llm, 'model') or None, role_file=role_path,
        timeout_seconds=number(llm, 'timeout_seconds', 90, 1, 300),
        batch_seconds=number(llm, 'batch_seconds', 2, 0, 30),
        context_characters=number(llm, 'context_characters', 30000, 2000, 100000, True),
        freshness_seconds=number(llm, 'freshness_seconds', 120, 5, 3600, True),
        reply_interval_seconds=number(llm, 'reply_interval_seconds', 10, 0, 300), introduction=introduction)
    return Configuration(path, fingerprint, credentials, meeting_no, password, directory.absolute(), scan,
        timeout_seconds=timeout,
        request_seconds=number(http, 'request_seconds', 0.7, 0.7, 30),
        poll_seconds=number(runtime, 'poll_seconds', 0.7, 0.7, 30),
        cooldown_seconds=number(runtime, 'cooldown_seconds', 30, 5, 3600),
        content_capacity_bytes=number(storage, 'content_capacity_bytes', 100_000_000, 10000, 1_000_000_000, True),
        retention_days=number(storage, 'retention_days', 7, 1, 365, True),
        diagnostic_bytes=number(storage, 'diagnostic_bytes', 1_000_000, 1000, 100_000_000, True),
        diagnostic_backups=number(storage, 'diagnostic_backups', 3, 1, 20, True),
        database_capacity_bytes=number(storage, 'database_capacity_bytes', 150_000_000, 1_000_000, 2_000_000_000, True),
        llm=llm_settings)
