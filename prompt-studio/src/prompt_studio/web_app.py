"""Application state behind the local web UI."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading

from .replay import DEFAULT_PACED_SECONDS, validate_paced_seconds
from .stack import PROMPT_FILES, Stack
from .transcript import Transcript, TranscriptError, parse_text
from .web import ACTIVE, WebError, Runner

PROMPT_LIMIT = 200_000
TEXT_LIMIT = 2_000_000


class StudioApp:
    def __init__(self, stack: Stack, prompt_dir: str | Path, owner: str):
        self.stack = stack
        self.prompt_dir = Path(prompt_dir)
        self.owner = owner
        self.runner = Runner(stack, owner)
        self._gateway_lock = threading.Lock()

    def close(self):
        self.runner.stop()

    def config(self) -> dict:
        return {
            'owner': self.owner,
            'backend': self.stack.config.backend,
            'model': self.stack.config.model,
            'prompt_dir': str(self.prompt_dir),
            'gateway_url': self.stack.gateway_url,
            'paced_seconds': DEFAULT_PACED_SECONDS,
            'active': self.runner.active,
        }

    def health(self) -> dict:
        return {'status': 'ok', 'active': self.runner.active}

    def prompts(self) -> dict:
        files = {}
        for name in PROMPT_FILES:
            path = self.prompt_dir / name
            try:
                files[name] = path.read_text(encoding='utf-8')
            except OSError as exc:
                raise WebError(500, 'prompt_read',
                               f'{name}: {exc}') from None
        return {'prompt_dir': str(self.prompt_dir), 'files': files}

    def parse(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise WebError(400, 'bad_request', '请求必须是 JSON 对象')
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip():
            raise WebError(400, 'bad_transcript', 'text 不能为空')
        if len(text) > TEXT_LIMIT:
            raise WebError(413, 'too_large', 'text 超过 2MB')
        try:
            transcript = parse_text(text)
            self._apply_fields(transcript, payload)
            transcript.validate()
        except TranscriptError as exc:
            raise WebError(400, 'bad_transcript', str(exc)) from None
        return transcript.to_dict()

    def start_run(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise WebError(400, 'bad_request', '请求必须是 JSON 对象')
        mode = payload.get('mode', 'auto')
        if mode not in ('auto', 'paced'):
            raise WebError(400, 'bad_mode', 'mode 只能是 auto 或 paced')
        try:
            paced_seconds = validate_paced_seconds(
                payload.get('paced_seconds', DEFAULT_PACED_SECONDS))
        except ValueError as exc:
            raise WebError(400, 'bad_paced_seconds', str(exc)) from None
        transcript = self._transcript(payload)
        try:
            self.runner.start(transcript, mode, paced_seconds)
        except TranscriptError as exc:
            raise WebError(400, 'bad_transcript', str(exc)) from None
        return self.runner.snapshot()

    def _transcript(self, payload: dict) -> Transcript:
        scene = payload.get('scene')
        if scene is not None:
            try:
                transcript = Transcript.from_dict(scene)
            except TranscriptError as exc:
                raise WebError(400, 'bad_transcript', str(exc)) from None
            self._apply_fields(transcript, payload)
            try:
                transcript.validate()
            except TranscriptError as exc:
                raise WebError(400, 'bad_transcript', str(exc)) from None
            return transcript
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip():
            raise WebError(400, 'bad_transcript', 'text 不能为空')
        if len(text) > TEXT_LIMIT:
            raise WebError(413, 'too_large', 'text 超过 2MB')
        try:
            transcript = parse_text(text)
            self._apply_fields(transcript, payload)
            transcript.validate()
        except TranscriptError as exc:
            raise WebError(400, 'bad_transcript', str(exc)) from None
        return transcript

    @staticmethod
    def _apply_fields(transcript: Transcript, payload: dict):
        title = payload.get('title')
        if title is not None:
            if not isinstance(title, str):
                raise WebError(400, 'bad_transcript', 'title 必须是字符串')
            transcript.title = title
        description = payload.get('description')
        if description is not None:
            if not isinstance(description, str):
                raise WebError(400, 'bad_transcript', 'description 必须是字符串')
            transcript.description = description

    def write_prompt(self, name: str, payload: object) -> dict:
        if name not in PROMPT_FILES:
            raise WebError(404, 'not_found', '未知提示词文件')
        if self.runner.active:
            raise WebError(409, 'run_active', '重放运行中不能修改提示词')
        if not isinstance(payload, dict):
            raise WebError(400, 'bad_request', '请求必须是 JSON 对象')
        content = payload.get('content')
        if not isinstance(content, str):
            raise WebError(400, 'bad_prompt', 'content 必须是字符串')
        if len(content) > PROMPT_LIMIT:
            raise WebError(413, 'too_large', '提示词超过 200KB')
        self._write_atomic(name, content)
        return {'name': name, 'bytes': len(content.encode('utf-8'))}

    def _write_atomic(self, name: str, content: str):
        target = self.prompt_dir / name
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                    'w', encoding='utf-8', dir=self.prompt_dir,
                    prefix=f'.{name}.', delete=False) as handle:
                temp_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, target)
        except OSError as exc:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise WebError(500, 'prompt_write', f'{name}: {exc}') from None

    def set_model(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise WebError(400, 'bad_request', '请求必须是 JSON 对象')
        model = payload.get('model')
        if not isinstance(model, str) or not model.strip():
            raise WebError(400, 'bad_model', 'model 不能为空')
        model = model.strip()
        if len(model) > 120 or any(
                char not in 'abcdefghijklmnopqrstuvwxyz'
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/-'
                for char in model):
            raise WebError(400, 'bad_model', 'model 含非法字符')
        if self.runner.active:
            raise WebError(409, 'run_active', '重放运行中不能切换模型')
        with self._gateway_lock:
            if model != self.stack.config.model:
                self.stack.restart_gateway(model)
        return self.config()
