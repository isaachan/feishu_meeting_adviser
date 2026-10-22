"""Strict local YAML configuration; spectator meetings are discovered at runtime."""
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
from typing import Any
import yaml

from .client import Credentials
from .collector import ListenSettings

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


@dataclass(frozen=True)
class Configuration:
    path: Path
    fingerprint: str
    credentials: Credentials
    data_directory: Path
    timeout_seconds: float = 10
    request_seconds: float = 0.7
    listen: ListenSettings = field(default_factory=ListenSettings)
    max_database_bytes: int = 150_000_000


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
    sections = set(raw)
    unknown = sections - {'app', 'http', 'listen', 'storage'}
    if unknown:
        raise ConfigError('unknown top-level configuration sections')
    app = ConfigSection('app', raw.get('app', {}))
    app.reject_unknown({'id', 'secret'})
    credentials = Credentials(app.string('id', required=True), app.string('secret', required=True))
    http = ConfigSection('http', raw.get('http', {}))
    http.reject_unknown({'timeout_seconds', 'request_seconds'})
    listen = ConfigSection('listen', raw.get('listen', {}))
    listen.reject_unknown(ListenSettings.__dataclass_fields__)
    storage = ConfigSection('storage', raw.get('storage', {}))
    storage.reject_unknown({'directory', 'max_database_bytes'})
    data_directory = Path(storage.string('directory', 'data'))
    if not data_directory.is_absolute():
        data_directory = path.parent / data_directory
    try:
        listen_settings = ListenSettings(
            poll_seconds=listen.number('poll_seconds', 1.0, 0.7, 30),
            cooldown_seconds=listen.number('cooldown_seconds', 30, 5, 3600),
            page_size=listen.number('page_size', 100, 1, 100, integer=True),
            max_pages_per_cycle=listen.number('max_pages_per_cycle', 10, 1, 100, integer=True),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    return Configuration(
        path=path, fingerprint=fingerprint, credentials=credentials,
        data_directory=data_directory.absolute(),
        timeout_seconds=http.number('timeout_seconds', 10, 1, 60),
        request_seconds=http.number('request_seconds', 0.7, 0.7, 30),
        listen=listen_settings,
        max_database_bytes=storage.number('max_database_bytes', 150_000_000,
                                          1_000_000, 2_000_000_000, integer=True),
    )
