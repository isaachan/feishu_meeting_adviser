"""Load all application settings from config.yaml and CLI arguments."""

from dataclasses import dataclass
from pathlib import Path
import yaml


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    executable: str
    model: str
    api_key: str | None
    auth_file: Path | None
    timeout_seconds: int
    max_context_bytes: int
    history_turns: int
    max_reply_characters: int
    queue_size: int


def _positive_int(section: dict, name: str) -> int:
    value = section.get(name)
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _section(data: dict, name: str) -> dict:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"config.yaml 缺少 {name} 配置段")
    return value


def _required(section: dict, name: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config.yaml 中 {name} 不能为空")
    return value.strip()


def load_settings(path: Path) -> Settings:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("config.yaml 必须是 YAML 对象")
    feishu = _section(data, "feishu")
    opencode = _section(data, "opencode")
    chat = _section(data, "chat")
    model = _required(opencode, "model")
    if model.count("/") != 1 or not all(model.split("/")):
        raise ValueError("opencode.model 必须采用 provider/model 格式")
    api_key = opencode.get("api_key", "")
    auth_file = opencode.get("auth_file", "")
    if not isinstance(api_key, str) or not isinstance(auth_file, str):
        raise ValueError("opencode.api_key 和 opencode.auth_file 必须是字符串")
    if bool(api_key.strip()) == bool(auth_file.strip()):
        raise ValueError("opencode.api_key 和 opencode.auth_file 必须且只能填写一项")
    auth_path = Path(auth_file.strip()) if auth_file.strip() else None
    if auth_path is not None and not auth_path.is_absolute():
        auth_path = (path.parent / auth_path).resolve()
    settings = Settings(
        app_id=_required(feishu, "app_id"),
        app_secret=_required(feishu, "app_secret"),
        executable=_required(opencode, "executable"),
        model=model,
        api_key=api_key.strip() or None,
        auth_file=auth_path,
        timeout_seconds=_positive_int(opencode, "timeout_seconds"),
        max_context_bytes=_positive_int(chat, "max_context_bytes"),
        history_turns=_positive_int(chat, "history_turns"),
        max_reply_characters=_positive_int(chat, "max_reply_characters"),
        queue_size=_positive_int(chat, "queue_size"),
    )
    return settings
