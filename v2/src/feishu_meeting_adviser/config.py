"""Local-only configuration with one strict, unreleased-product schema."""
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
from typing import Any
import yaml
from .client import Credentials
from .collector import ScanSettings
from .llm import LLMSettings

DEFAULT_CONFIG = Path('config.yaml')


class ConfigError(Exception):
    pass


class ConfigSection:
    def __init__(self, name: str, values: dict[str, Any]):
        self.name = name
        self.values = values

    def string(self, key, default='', *, required=False):
        value = self.values.get(key, default)
        if not isinstance(value, str) or (required and not value.strip()):
            raise ConfigError(f'invalid configuration field: {key}')
        return value

    def number(self, key, default, low, high, *, integer=False):
        value = self.values.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high
                or integer and type(value) is not int):
            raise ConfigError(f'invalid numeric configuration field: {key}')
        return value

    def reject_unknown(self, fields):
        if set(self.values) - set(fields):
            raise ConfigError(f'unknown {self.name} configuration fields')


class ConfigDocument:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw

    def section(self, name):
        value = self.raw.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f'{name} must be a mapping')
        return ConfigSection(name, value)

    def reject_unknown(self, sections):
        if set(self.raw) - set(sections):
            raise ConfigError('unknown top-level configuration sections')


@dataclass(frozen=True)
class StorageSettings:
    directory: Path
    content_capacity_bytes: int
    retention_days: int
    diagnostic_bytes: int
    diagnostic_backups: int
    database_capacity_bytes: int

    @classmethod
    def parse(cls, section: ConfigSection, base_directory: Path):
        section.reject_unknown(cls.__dataclass_fields__)
        directory = Path(section.string('directory', 'data'))
        if not directory.is_absolute():
            directory = base_directory / directory
        return cls(
            directory.absolute(),
            section.number('content_capacity_bytes', 100_000_000, 10000,
                           1_000_000_000, integer=True),
            section.number('retention_days', 7, 1, 365, integer=True),
            section.number('diagnostic_bytes', 1_000_000, 1000,
                           100_000_000, integer=True),
            section.number('diagnostic_backups', 3, 1, 20, integer=True),
            section.number('database_capacity_bytes', 150_000_000, 1_000_000,
                           2_000_000_000, integer=True),
        )


@dataclass(frozen=True)
class HTTPSettings:
    timeout_seconds: float
    request_seconds: float

    @classmethod
    def parse(cls, section: ConfigSection):
        section.reject_unknown(cls.__dataclass_fields__)
        return cls(
            section.number('timeout_seconds', 10, 1, 60),
            section.number('request_seconds', 0.7, 0.7, 30),
        )


@dataclass(frozen=True)
class RuntimeSettings:
    poll_seconds: float
    cooldown_seconds: float

    @classmethod
    def parse(cls, section: ConfigSection):
        section.reject_unknown(cls.__dataclass_fields__)
        return cls(
            section.number('poll_seconds', 0.7, 0.7, 30),
            section.number('cooldown_seconds', 30, 5, 3600),
        )


@dataclass(frozen=True)
class MeetingContextSettings:
    calendar_id: str = ''
    identity: str = 'user'
    context_characters: int = 10000
    max_documents: int = 10
    timeout_seconds: float = 60
    binding_tolerance_seconds: int = 3600
    preview_bytes: int = 1_000_000
    preview_backups: int = 3

    @property
    def enabled(self):
        return bool(self.calendar_id)

    @classmethod
    def parse(cls, section: ConfigSection):
        section.reject_unknown(cls.__dataclass_fields__)
        calendar_id = section.string('calendar_id')
        if calendar_id and (not re.fullmatch(r'[A-Za-z0-9_.@-]{1,256}', calendar_id)
                            or calendar_id in ('.', '..')):
            raise ConfigError('invalid meeting_context resource identifier')
        identity = section.string('identity', 'user')
        if identity not in ('user', 'application'):
            raise ConfigError('meeting_context.identity must be user or application')
        return cls(
            calendar_id=calendar_id,
            identity=identity,
            context_characters=section.number(
                'context_characters', 10000, 1000, 50000, integer=True),
            max_documents=section.number('max_documents', 10, 1, 10, integer=True),
            timeout_seconds=section.number('timeout_seconds', 60, 5, 300),
            binding_tolerance_seconds=section.number(
                'binding_tolerance_seconds', 3600, 0, 86400, integer=True),
            preview_bytes=section.number(
                'preview_bytes', 1_000_000, 10000, 100_000_000, integer=True),
            preview_backups=section.number('preview_backups', 3, 1, 20, integer=True),
        )


def _parse_llm(section: ConfigSection, base_directory: Path):
    section.reject_unknown(LLMSettings.__dataclass_fields__)
    backend = section.string('backend', 'rule')
    if backend not in ('rule', 'codex', 'opencode'):
        raise ConfigError('llm.backend must be rule, codex or opencode')
    opencode_model = section.string(
        'opencode_model', 'zai-coding-plan/glm-5.3', required=True)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', opencode_model):
        raise ConfigError('llm.opencode_model must be provider/model')
    role_file = section.string('role_file')
    role_path = Path(role_file) if role_file else None
    if role_path and not role_path.is_absolute():
        role_path = base_directory / role_path
    introduction = section.values.get('introduction', True)
    if type(introduction) is not bool:
        raise ConfigError('llm.introduction must be boolean')
    return LLMSettings(
        backend=backend,
        executable=section.string('executable', 'codex', required=True),
        model=section.string('model') or None,
        role_file=role_path,
        timeout_seconds=section.number('timeout_seconds', 90, 1, 300),
        batch_seconds=section.number('batch_seconds', 2, 0, 30),
        context_characters=section.number(
            'context_characters', 30000, 2000, 100000, integer=True),
        freshness_seconds=section.number(
            'freshness_seconds', 120, 5, 3600, integer=True),
        reply_interval_seconds=section.number('reply_interval_seconds', 10, 0, 300),
        introduction=introduction,
        opencode_executable=section.string(
            'opencode_executable', 'opencode', required=True),
        opencode_model=opencode_model,
    )


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
    meeting_context: MeetingContextSettings = field(default_factory=MeetingContextSettings)


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
    document = ConfigDocument(raw)
    document.reject_unknown({
        'app', 'meeting', 'http', 'collector', 'storage', 'runtime', 'llm',
        'meeting_context',
    })
    app = document.section('app')
    app.reject_unknown({'id', 'secret'})
    credentials = Credentials(
        app.string('id', required=True), app.string('secret', required=True))
    meeting = document.section('meeting')
    meeting.reject_unknown({'meeting_no', 'password'})
    meeting_no = meeting.string('meeting_no')
    if len(meeting_no) != 9 or not meeting_no.isascii() or not meeting_no.isdecimal():
        raise ConfigError('meeting.meeting_no must be a quoted nine-digit string')
    password = meeting.string('password')
    collector = document.section('collector')
    collector.reject_unknown(ScanSettings.__dataclass_fields__)
    try:
        scan = ScanSettings(**collector.values)
    except (ValueError, TypeError):
        raise ConfigError('invalid collector configuration') from None
    storage = StorageSettings.parse(document.section('storage'), path.parent)
    http = HTTPSettings.parse(document.section('http'))
    runtime = RuntimeSettings.parse(document.section('runtime'))
    llm = _parse_llm(document.section('llm'), path.parent)
    context = MeetingContextSettings.parse(document.section('meeting_context'))
    return Configuration(
        path, fingerprint, credentials, meeting_no, password, storage.directory, scan,
        timeout_seconds=http.timeout_seconds,
        request_seconds=http.request_seconds,
        poll_seconds=runtime.poll_seconds,
        cooldown_seconds=runtime.cooldown_seconds,
        content_capacity_bytes=storage.content_capacity_bytes,
        retention_days=storage.retention_days,
        diagnostic_bytes=storage.diagnostic_bytes,
        diagnostic_backups=storage.diagnostic_backups,
        database_capacity_bytes=storage.database_capacity_bytes,
        llm=llm,
        meeting_context=context,
    )
