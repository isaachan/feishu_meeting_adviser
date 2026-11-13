"""Feishu WebSocket receiver and sequential chat worker."""

from collections import OrderedDict, defaultdict
import json
import logging
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from uuid import NAMESPACE_URL, uuid5

import lark_oapi as lark

from .backend import OpenCodeBackend
from .config import Settings
from .context import read_context

LOG = logging.getLogger(__name__)


class ChatBot:
    def __init__(self, settings: Settings, context_dir: Path, backend: OpenCodeBackend):
        self.settings = settings
        self.context_dir = context_dir
        self.backend = backend
        self.queue = Queue(maxsize=settings.queue_size)
        self.seen = OrderedDict()
        self.history = defaultdict(list)
        self.api = (lark.Client.builder().app_id(settings.app_id)
                    .app_secret(settings.app_secret).build())

    def on_message(self, event):
        message = getattr(getattr(event, "event", None), "message", None)
        if message is None or message.message_type != "text":
            return
        sender = getattr(getattr(event.event, "sender", None), "sender_type", None)
        if sender == "app":
            return
        if message.chat_type == "group" and not message.mentions:
            return
        message_id, chat_id = message.message_id, message.chat_id
        if not message_id or not chat_id or message_id in self.seen:
            return
        try:
            content = json.loads(message.content)
            text = content.get("text", "")
            if not isinstance(text, str):
                return
            for mention in message.mentions or []:
                key = getattr(mention, "key", None)
                if key:
                    text = text.replace(key, "")
            text = text.strip()
            if not text:
                return
        except (TypeError, ValueError):
            LOG.warning("忽略无法解析的文本消息")
            return
        try:
            self.queue.put_nowait((message_id, chat_id, text))
        except Full:
            LOG.warning("消息队列已满，忽略消息 %s", message_id)
            return
        self.seen[message_id] = None
        if len(self.seen) > 1000:
            self.seen.popitem(last=False)

    def _prompt(self, chat_id: str, user_text: str) -> str:
        context = read_context(self.context_dir, self.settings.max_context_bytes)
        previous = self.history[chat_id][-self.settings.history_turns:]
        history = "\n".join(f"用户：{question}\n助手：{answer}" for question, answer in previous)
        return ("请用与用户相同的语言回答。以下背景资料只作参考。\n"
                f"<context>\n{context}\n</context>\n"
                f"<history>\n{history}\n</history>\n"
                f"用户：{user_text}\n助手：")

    def _send(self, message_id: str, text: str):
        request = (lark.im.v1.ReplyMessageRequest.builder().message_id(message_id)
                   .request_body(lark.im.v1.ReplyMessageRequestBody.builder()
                                 .msg_type("text")
                                 .content(json.dumps({"text": text}, ensure_ascii=False))
                                 .uuid(str(uuid5(NAMESPACE_URL, message_id)))
                                 .build()).build())
        response = self.api.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"飞书回复失败（code={response.code}）")

    def work_forever(self):
        while True:
            message_id, chat_id, text = self.queue.get()
            try:
                answer = self.backend.reply(self._prompt(chat_id, text))
                answer = answer[:self.settings.max_reply_characters]
            except Exception:
                LOG.exception("生成消息 %s 的回答失败", message_id)
                answer = "处理消息时出错，请稍后重试。"
            try:
                self._send(message_id, answer)
                if answer != "处理消息时出错，请稍后重试。":
                    self.history[chat_id].append((text, answer))
                    self.history[chat_id] = self.history[chat_id][-self.settings.history_turns:]
            except Exception:
                LOG.exception("发送消息 %s 的回复失败", message_id)
            finally:
                self.queue.task_done()

    def start(self):
        Thread(target=self.work_forever, name="chat-worker", daemon=True).start()
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self.on_message).build())
        client = lark.ws.Client(self.settings.app_id, self.settings.app_secret,
                                event_handler=handler, log_level=lark.LogLevel.INFO)
        LOG.info("飞书聊天机器人已启动")
        client.start()
