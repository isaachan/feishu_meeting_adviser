"""Auth node configuration: app credentials from env or config.json."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_PORT, MAX_FILE_BYTES
from .errors import AuthError

ENV_APP_ID = 'AUTH_FEISHU_APP_ID'
ENV_APP_SECRET = 'AUTH_FEISHU_APP_SECRET'


@dataclass(frozen=True, slots=True)
class FeishuAppCredentials:
    app_id: str
    app_secret: str


def load_credentials(directory: Path) -> FeishuAppCredentials:
    """Env vars win; otherwise ``config.json`` in the data directory."""
    app_id = os.environ.get(ENV_APP_ID)
    app_secret = os.environ.get(ENV_APP_SECRET)
    if not (app_id and app_secret):
        config_path = Path(directory) / 'config.json'
        try:
            value = json.loads(config_path.read_bytes()[:MAX_FILE_BYTES])
        except (OSError, ValueError):
            value = None
        if isinstance(value, dict):
            app_id = app_id or value.get('app_id')
            app_secret = app_secret or value.get('app_secret')
    if (not isinstance(app_id, str) or not isinstance(app_secret, str)
            or not app_id or not app_secret):
        raise AuthError('oauth_not_authorized')
    return FeishuAppCredentials(app_id, app_secret)


@dataclass(frozen=True, slots=True)
class Config:
    credentials: FeishuAppCredentials
    data_dir: Path
    port: int = DEFAULT_PORT
    base_url: str = 'http://127.0.0.1:8381'
    state_ttl_s: int = 600


def load_config(data_dir: Path, *, port=None, base_url=None, state_ttl_s=None) -> Config:
    return Config(
        credentials=load_credentials(data_dir),
        data_dir=Path(data_dir),
        port=DEFAULT_PORT if port is None else port,
        base_url='http://127.0.0.1:8381' if base_url is None else base_url,
        state_ttl_s=600 if state_ttl_s is None else state_ttl_s)
